"""工作流门面：供 API / 前端 / 测试调用。线程 ID = RFQ 编号。"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from langgraph.types import Command

from app.graph.builder import build_graph
from app.graph.nodes import SNAP_TABLES, now, snap_name, summarize
from app.memory.gate import Candidate, MemoryGate, Verdict, injection_marks
from app.parsers.email_parser import parse_eml
from app.pricing.engine import PricingEngine
from app.pricing.rules import Rules
from app.schemas import MatchedItem
from app.services import Services
from app.vcs.base import ident


def new_rfq_id() -> str:
    return f"RFQ{datetime.now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:4]}"


class RFQWorkflow:
    def __init__(self, svc: Services, checkpointer=None) -> None:
        self.svc = svc
        self.graph = build_graph(svc, checkpointer)

    @staticmethod
    def _cfg(rfq_id: str) -> dict:
        return {"configurable": {"thread_id": rfq_id}}

    def _work_dir(self, rfq_id: str) -> Path:
        s = self.svc.settings
        return s.path(f"{s.upload_dir}/{rfq_id}/.keep").parent

    # ---------- 运行 ----------
    def start(self, *, subject: str = "", sender: str = "", body: str = "", attachments: list[str] | None = None,
              rfq_id: str | None = None) -> dict:
        rfq_id = rfq_id or new_rfq_id()
        self.graph.invoke({"rfq_id": rfq_id, "subject": subject, "sender": sender, "body": body,
                           "attachments": attachments or [], "work_dir": str(self._work_dir(rfq_id))},
                          self._cfg(rfq_id))
        return self.view(rfq_id)

    def start_eml(self, raw: bytes, extra_attachments: list[str] | None = None) -> dict:
        rfq_id = new_rfq_id()
        mail = parse_eml(raw, self._work_dir(rfq_id) / "attachments")
        return self.start(subject=mail.subject, sender=mail.sender, body=mail.body,
                          attachments=mail.attachments + (extra_attachments or []), rfq_id=rfq_id)

    def resume(self, rfq_id: str, decision: dict) -> dict:
        if not self.pending(rfq_id):
            raise ValueError(f"{rfq_id} 不在待审批状态")
        self.graph.invoke(Command(resume=decision), self._cfg(rfq_id))
        return self.view(rfq_id)

    # ---------- 查询 ----------
    def pending(self, rfq_id: str) -> dict | None:
        snap = self.graph.get_state(self._cfg(rfq_id))
        for t in snap.tasks:
            for it in t.interrupts:
                return it.value
        return None

    def view(self, rfq_id: str) -> dict:
        snap = self.graph.get_state(self._cfg(rfq_id))
        values = dict(snap.values or {})
        rec = self.svc.db.query_one("SELECT * FROM rfq_record WHERE rfq_id=%s", (rfq_id,))
        if not values and not rec:
            raise KeyError(rfq_id)
        values.pop("body", None)
        return {"rfq_id": rfq_id, "record": rec, "state": values, "next": list(snap.next),
                "pending": self.pending(rfq_id),
                "committed": self.svc.db.query("SELECT * FROM quote_line WHERE rfq_id=%s ORDER BY line_no", (rfq_id,))}

    def list(self, limit: int = 50) -> list[dict]:
        return self.svc.db.query(f"SELECT rfq_id, customer_id, subject, status, created_at, updated_at FROM rfq_record "
                                 f"ORDER BY created_at DESC LIMIT {int(limit)}")

    def branch_compare(self, rfq_id: str) -> list[dict]:
        """三档策略逐行价格/毛利/赢单概率对比。"""
        st = self.graph.get_state(self._cfg(rfq_id)).values or {}
        quotes = st.get("quotes") or {}
        rows: dict[str, dict] = {}
        for s, lines in quotes.items():
            for l in lines:
                r = rows.setdefault(l["line_key"], {"line_key": l["line_key"], "line_no": l["line_no"],
                                                    "sku": l["sku"], "qty": l["qty"], "unit_cost": l["unit_cost"]})
                r[f"{s}.price"], r[f"{s}.margin"], r[f"{s}.win"] = l["unit_price"], l["margin"], l["win_prob"]
        return sorted(rows.values(), key=lambda r: r["line_no"])

    # ---------- 时间旅行：按报价当时的数据快照重放，对比当前数据 ----------
    def _matched(self, rfq_id: str) -> tuple[dict, list[dict] | None]:
        st = self.graph.get_state(self._cfg(rfq_id)).values or {}
        matched = st.get("matched")
        if matched is None:  # 进程重启后 checkpoint 丢失：从审计日志恢复
            row = next((r for r in self.svc.audit.list(rfq_id) if r["node"] == "normalize"), None) or {}
            out = row.get("output_json")
            matched = out.get("matched") if isinstance(out, dict) else None
        return st, matched

    def replay(self, rfq_id: str, strategy: str | None = None) -> dict:
        st, matched = self._matched(rfq_id)
        if not matched:
            if not self.svc.db.query_one("SELECT rfq_id FROM rfq_record WHERE rfq_id=%s", (rfq_id,)):
                raise KeyError(rfq_id)
            raise ValueError(f"{rfq_id} 无可重放的物料数据")
        rec = self.svc.db.query_one("SELECT customer_id, header_json FROM rfq_record WHERE rfq_id=%s", (rfq_id,)) or {}
        customer = self.svc.db.query_one("SELECT * FROM customer WHERE customer_id=%s", (rec.get("customer_id") or "",))
        strategy = strategy or (st.get("decision") or {}).get("strategy") or st.get("recommended") or "balanced"
        currency = (st.get("header") or {}).get("currency") or "USD"

        vcs = self.svc.vcs
        rules_then = vcs.read_at("pricing_rule", snap_name(rfq_id, "pricing_rule"))
        costs_then = vcs.read_at("cost_price", snap_name(rfq_id, "cost_price"))
        then = PricingEngine(self.svc.db, Rules(rows=rules_then), costs_then)
        cur = PricingEngine(self.svc.db, Rules(self.svc.db))
        lines = []
        for m in (MatchedItem(**x) for x in matched):
            if not m.sku:
                continue
            a = then.price(rfq_id, m, then.evidence(m.sku, rec.get("customer_id")), customer, strategy, currency)
            b = cur.price(rfq_id, m, cur.evidence(m.sku, rec.get("customer_id")), customer, strategy, currency)
            lines.append({"line_no": m.line_no, "sku": m.sku, "cost_then": a.unit_cost, "cost_now": b.unit_cost,
                          "price_then": a.unit_price, "price_now": b.unit_price,
                          "delta_pct": round(b.unit_price / a.unit_price - 1, 4) if a.unit_price else None})
        kv_then = {r["rule_key"]: r["rule_value"] for r in rules_then}
        kv_now = {r["rule_key"]: r["rule_value"] for r in self.svc.db.query("SELECT rule_key, rule_value FROM pricing_rule")}
        rule_diff = [{"rule_key": k, "then": kv_then.get(k), "now": kv_now.get(k)}
                     for k in sorted(set(kv_then) | set(kv_now)) if kv_then.get(k) != kv_now.get(k)]
        return {"rfq_id": rfq_id, "strategy": strategy, "snapshots": [snap_name(rfq_id, t) for t in SNAP_TABLES],
                "rule_diff": rule_diff, "lines": lines}

    # ---------- 规则沙盒：在 pricing_rule 数据分支上改规则，用近期询价回测，满意再合并 ----------
    def rules_branch(self, name: str, changes: dict[str, str]) -> dict:
        b = ident(f"rule_{name}")
        self.svc.vcs.drop_branch(b)
        self.svc.vcs.branch(b, "pricing_rule")
        changes = {"version": name[:16], **changes}  # 合并后决策审计中的 rule_ver 随之变化
        for k, v in changes.items():
            if self.svc.db.query_one(f"SELECT rule_key FROM {b} WHERE rule_key=%s", (k,)):
                self.svc.db.execute(f"UPDATE {b} SET rule_value=%s WHERE rule_key=%s", (str(v), k))
            else:
                self.svc.db.execute(f"INSERT INTO {b} VALUES (%s,%s,%s,%s)", (k, str(v), name[:16], "沙盒新增"))
        return {"branch": b, "diff": asdict(self.svc.vcs.diff(b, "pricing_rule"))}

    def rules_backtest(self, branch: str, limit: int = 10, strategy: str = "balanced") -> dict:
        db = self.svc.db
        main = PricingEngine(db, Rules(db))
        test = PricingEngine(db, Rules(rows=db.query(f"SELECT rule_key, rule_value FROM {ident(branch)}")))
        out = []
        for rec in self.list(200):
            if len(out) >= limit:
                break
            _, matched = self._matched(rec["rfq_id"])
            if not matched:
                continue
            cust = db.query_one("SELECT * FROM customer WHERE customer_id=%s", (rec["customer_id"] or "",))
            cur = json.loads(db.query_one("SELECT header_json FROM rfq_record WHERE rfq_id=%s",
                                          (rec["rfq_id"],))["header_json"] or "{}").get("currency") or "USD"
            res = {}
            for tag, eng in (("main", main), ("branch", test)):
                lines = [eng.price(rec["rfq_id"], m, eng.evidence(m.sku, rec["customer_id"]), cust, strategy, cur)
                         .model_dump() for m in (MatchedItem(**x) for x in matched) if m.sku]
                res[tag] = summarize(lines)
            out.append({"rfq_id": rec["rfq_id"], "customer_id": rec["customer_id"], "currency": cur,
                        "main": res["main"], "branch": res["branch"]})
        tot = {t: {"amount": sum(r[t]["amount"] for r in out), "expected_profit": sum(r[t]["expected_profit"] for r in out)}
               for t in ("main", "branch")}
        return {"branch": branch, "strategy": strategy, "rfqs": out, "total": tot}

    # ---------- 成交反馈：回写历史 + 经验先进记忆分支，评审后再合并 ----------
    def record_outcome(self, rfq_id: str, won: bool, deal_prices: dict[str, float] | None = None,
                       note: str = "") -> dict:
        db, mem = self.svc.db, self.svc.memory
        rec = db.query_one("SELECT * FROM rfq_record WHERE rfq_id=%s", (rfq_id,))
        if not rec or rec["status"] not in ("quoted", "won", "lost"):
            raise ValueError(f"{rfq_id} 尚未完成报价")
        lines = db.query("SELECT * FROM quote_line WHERE rfq_id=%s", (rfq_id,))
        deal_prices = deal_prices or {}
        db.execute("DELETE FROM quote_history WHERE id LIKE %s", (f"{rfq_id}:%",))
        db.executemany("INSERT INTO quote_history VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", [
            (l["line_key"], rec["customer_id"], l["sku"], l["qty"], l["unit_price"], l["currency"], now(), int(won),
             deal_prices.get(l["line_key"], l["unit_price"]) if won else None) for l in lines if l["sku"]])
        status = "won" if won else "lost"
        db.execute("UPDATE rfq_record SET status=%s, updated_at=%s WHERE rfq_id=%s", (status, now(), rfq_id))

        marks = injection_marks(note)
        if marks:  # 备注常由客户反馈转述而来，同样过 MemoryGate：命中注入特征则丢弃备注、记录拦截
            v = Verdict("block", "high", [f"成交备注含提示注入特征：{'、'.join(marks)}"],
                        Candidate(note, "injection", f"outcome:{rfq_id}", rec["customer_id"], injection=marks))
            MemoryGate(self.svc).record(rfq_id, v, None, "blocked")
            note = ""
        branch = f"learn_{rfq_id.lower()}"
        mem.branch(branch)
        with mem.on_branch(branch):
            strategy = (self.graph.get_state(self._cfg(rfq_id)).values.get("decision") or {}).get("strategy", "?")
            mem.store(f"RFQ {rfq_id} 结果：{'成交' if won else '丢单'}，采用策略 {strategy}。{note}".strip(),
                      kind="episodic", customer_id=rec["customer_id"], trust_tier="T2", source=f"outcome:{rfq_id}")
        self.svc.audit.write(rfq_id, "outcome", output={"status": status, "deal_prices": deal_prices, "note": note,
                                                        "memory_branch": branch, "note_blocked": marks},
                             human_action=status)
        return {"rfq_id": rfq_id, "status": status, "memory_branch": branch, "memory_diff": mem.diff(branch),
                "note_blocked": marks}
