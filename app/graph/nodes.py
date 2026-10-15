"""Agent 节点。每个节点由 AuditLogger.wrap 包装，模型调用/证据/人工操作自动写入 decision_log。"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from langgraph.types import interrupt

from app.audit.hook import add_evidence, set_confidence, set_human, set_prompt
from app.memory.base import MemoryItem, trusted
from app.memory.gate import MemoryGate
from app.parsers.dispatcher import parse_attachment
from app.parsers.email_parser import rule_body_items, rule_header, sender_domain
from app.pricing.engine import PricingEngine
from app.pricing.matcher import SkuMatcher
from app.pricing.risk import assess
from app.pricing.rules import Rules
from app.prompts import (BODY_ITEMS_SYSTEM, INTAKE_SYSTEM, PROMPT_VER, REPLY_SYSTEM, VISION_ITEMS_SYSTEM,
                         ReplyDraft)
from app.render import clarification_rule, render_excel, render_html, render_pdf, reply_rule
from app.router.router import RULE_ENGINE, RouteContext
from app.schemas import (STRATEGIES, STRATEGY_LABEL, ApprovalDecision, LineItem, LineItemList, MatchedItem,
                         QuoteLine, RFQHeader, RiskItem)
from app.services import Services
from app.vcs.base import ident

QL_COLS = list(QuoteLine.model_fields)
SNAP_TABLES = ("cost_price", "pricing_rule")
_ADJ = re.compile(r"sku=(\S+)\s+adj=([-+]?\d*\.?\d+)")
_HINTS = ((re.compile(r"价格敏感|比价|price[- ]sensitive", re.I), "win_first"),
          (re.compile(r"不敏感|看重(质量|交期)|quality first", re.I), "margin_first"))


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def branch_name(rfq_id: str, strategy: str) -> str:
    return ident(f"ql_{rfq_id.lower()}_{strategy}")


def snap_name(rfq_id: str, table: str) -> str:
    return ident(f"sn_{rfq_id.lower()}_{table}")


def _mem(m: MemoryItem) -> dict:
    return {"memory_id": m.memory_id, "kind": m.memory_type, "trust": m.trust_tier,
            "source": m.source, "score": m.score, "content": m.content[:300]}


def memory_adjustments(mems: list[MemoryItem], weight: float, cap: float) -> dict[str, float]:
    """从人工改价经验记忆中提取 SKU 级调价比例：均值 × 权重，截断到 ±cap。"""
    acc: dict[str, list[float]] = {}
    for m in mems:
        for sku, v in _ADJ.findall(m.content):
            acc.setdefault(sku, []).append(float(v))
    return {k: round(max(-cap, min(cap, weight * sum(v) / len(v))), 4) for k, v in acc.items()}


def summarize(lines: list[dict]) -> dict:
    amount = sum(l["unit_price"] * l["qty"] for l in lines)
    cost = sum(l["unit_cost"] * l["qty"] for l in lines)
    exp = sum(l["win_prob"] * (l["unit_price"] - l["unit_cost"]) * l["qty"] for l in lines if l["unit_price"])
    priced = [l for l in lines if l["unit_price"]]
    return {"amount": round(amount, 2), "margin": round((amount - cost) / amount, 4) if amount else 0.0,
            "expected_profit": round(exp, 2), "lines": len(lines), "priced": len(priced),
            "avg_win_prob": round(sum(l["win_prob"] for l in priced) / len(priced), 3) if priced else 0.0}


class RFQNodes:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.db = svc.db

    def _status(self, rfq_id: str, status: str) -> None:
        self.db.execute("UPDATE rfq_record SET status=%s, updated_at=%s WHERE rfq_id=%s", (status, now(), rfq_id))

    # ---------- 1. 询价接收：识别客户、抽取单头、冻结数据与记忆快照 ----------
    def intake(self, st: dict) -> dict:
        rfq, subject, body, sender = st["rfq_id"], st.get("subject", ""), st.get("body", ""), st.get("sender", "")
        domain = sender_domain(sender)
        customer = self.db.query_one("SELECT * FROM customer WHERE email_domain=%s", (domain,)) if domain else None
        set_prompt(PROMPT_VER["intake_extract"])
        r = self.svc.router.run(
            "intake_extract", RFQHeader, INTAKE_SYSTEM, f"Subject: {subject}\nFrom: {sender}\n\n{body[:6000]}",
            rule_fn=lambda: rule_header(subject, body, sender),
            ctx=RouteContext(high_value=(customer or {}).get("tier") == "strategic"))
        h: RFQHeader = r.output
        rules = Rules(self.db)
        h.sender_email = h.sender_email or sender
        if customer:
            h.customer_name = h.customer_name or customer["name"]
            h.payment_terms = h.payment_terms or customer.get("payment_terms")
        h.currency = (h.currency or rules.text("default_currency", "USD")).upper()

        # 报价即提交：先冻结本单依赖的成本表与规则表，任何时候都能按快照重放
        for t in SNAP_TABLES:
            self.svc.vcs.snapshot(snap_name(rfq, t), t)
        data_snap = f"sn_{rfq.lower()}"
        mem_snap = self.svc.memory.snapshot(f"m_{rfq.lower()}", f"{rfq} intake")

        cid = (customer or {}).get("customer_id")
        self.db.execute("DELETE FROM rfq_record WHERE rfq_id=%s", (rfq,))
        self.db.execute("INSERT INTO rfq_record VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (rfq, cid, subject[:500], "parsing", h.model_dump_json(), data_snap, mem_snap, now(), now()))
        add_evidence(sender_domain=domain, customer_id=cid, route=r.attempts,
                     data_snapshots=[snap_name(rfq, t) for t in SNAP_TABLES])
        return {"customer": customer, "header": h.model_dump(), "status": "parsing",
                "clarification": clarification_rule(h.model_dump(), h.missing_fields) if h.missing_fields else None,
                "data_snapshot": data_snap, "memory_snapshot": mem_snap}

    # ---------- 2. 附件/正文解析 ----------
    def parse(self, st: dict) -> dict:
        work = Path(st.get("work_dir") or self.svc.settings.path(f"{self.svc.settings.upload_dir}/{st['rfq_id']}/w").parent)
        work.mkdir(parents=True, exist_ok=True)
        items: list[LineItem] = []
        notes, images, complexity, texts = [], [], "normal", []
        for p in st.get("attachments") or []:
            res = parse_attachment(p, work)
            items += res.items
            notes += [f"{res.file}: {n}" for n in res.notes]
            images += res.images
            complexity = "high" if res.complexity == "high" else complexity
            texts.append({"file": res.file, "text": res.text[:20000], "hidden": res.hidden[:5000]})
        if images:
            set_prompt(PROMPT_VER["vision_items"])
            r = self.svc.router.run("vision_items", LineItemList, VISION_ITEMS_SYSTEM, "识别图片中的询价物料表格，按行输出。",
                                    rule_fn=lambda: LineItemList(), ctx=RouteContext(complexity="high", images=images[:4]))
            if r.model == RULE_ENGINE:
                notes.append(f"{len(images)} 页扫描件/图片无法识别（视觉模型不可用），需人工补录")
            items += [i.model_copy(update={"source": i.source or "vision"}) for i in r.output.items]
        body = st.get("body", "")
        if not items and body.strip():
            set_prompt(PROMPT_VER["body_items"])
            rb = rule_body_items(body)
            r = self.svc.router.run("body_items", LineItemList, BODY_ITEMS_SYSTEM, body[:8000],
                                    rule_fn=lambda: LineItemList(items=rb, confidence=0.75 if rb else 0.3))
            items = [i.model_copy(update={"source": i.source or "email-body"}) for i in r.output.items]
        elif items:
            set_confidence(min(i.confidence for i in items))
        if not items:
            notes.append("未识别到任何物料行")
            set_confidence(0.0)
        items = [i.model_copy(update={"line_no": n}) for n, i in enumerate(items, 1)]
        add_evidence(attachments=[Path(p).name for p in st.get("attachments") or []], item_count=len(items),
                     notes=notes, complexity=complexity)
        return {"items": [i.model_dump() for i in items], "parse_notes": notes, "complexity": complexity,
                "attachment_texts": texts}

    # ---------- 2b. 记忆网关：外部输入中的长期声明只进隔离分支，注入/矛盾/无证据的直接拦截 ----------
    def memory_gate(self, st: dict) -> dict:
        rfq, cid = st["rfq_id"], (st.get("customer") or {}).get("customer_id")
        texts = [("email:body", st.get("body", ""), False)]
        for t in st.get("attachment_texts") or []:
            texts += [(f"attachment:{t['file']}", t["text"], False), (f"attachment:{t['file']}#hidden", t["hidden"], True)]
        res = MemoryGate(self.svc).screen(rfq, cid, texts)
        add_evidence(memory_gate={k: res[k] for k in ("inbox", "block", "review", "skip", "injection")},
                     verdicts=res["verdicts"])
        if res["block"]:
            set_confidence(0.5)
        return {"gate": res}

    # ---------- 3. 物料标准化（含记忆中的料号映射） ----------
    def normalize(self, st: dict) -> dict:
        cid = (st.get("customer") or {}).get("customer_id")
        items = [LineItem(**i) for i in st.get("items", [])]
        query = " ".join(filter(None, [*(i.part_no for i in items), *(i.description for i in items)])) or "alias"
        mems = trusted(self.svc.memory.retrieve(query, customer_id=cid, top_k=20, kinds=["alias"]), "T3") \
            if items else []
        matcher = SkuMatcher(self.db)
        matched = [matcher.match(i, mems) for i in items]
        add_evidence(match_stats=dict(Counter(m.match_method for m in matched)), alias_memories=[_mem(m) for m in mems])
        if matched:
            set_confidence(sum(m.match_score for m in matched) / len(matched))
        return {"matched": [m.model_dump() for m in matched], "memories": [_mem(m) for m in mems]}

    # ---------- 4. 策略定价：三档策略各开一个 quote_line 数据分支 ----------
    def price(self, st: dict) -> dict:
        rfq, customer = st["rfq_id"], st.get("customer")
        cid = (customer or {}).get("customer_id")
        header = RFQHeader(**st["header"])
        rules = Rules(self.db)
        eng = PricingEngine(self.db, rules)
        matched = [MatchedItem(**m) for m in st.get("matched", [])]
        evidence = {}
        for m in matched:
            if m.sku and m.sku not in evidence and (ev := eng.evidence(m.sku, cid)):
                evidence[m.sku] = ev

        skus = sorted({m.sku for m in matched if m.sku})
        mems = trusted(self.svc.memory.retrieve(" ".join(skus) + " 人工改价 报价偏好", customer_id=cid, top_k=20,
                                                kinds=["procedural", "profile"]), "T3") if matched else []
        # 纵深防御：能直接改价格的只有 T1/T2 的改价经验（系统内人工审批产生），外部来源再怎么评审也到不了这里
        adj = memory_adjustments(trusted([m for m in mems if m.memory_type == "procedural"], "T2"),
                                 rules.get("memory_weight", 0.5), rules.get("memory_adj_cap", 0.1))

        branches, quotes, diffs, summary = {}, {}, {}, {}
        ph = ", ".join(["%s"] * len(QL_COLS))
        for s in STRATEGIES:
            b = branch_name(rfq, s)
            self.svc.vcs.drop_branch(b)  # 幂等：节点重跑时先清理
            self.svc.vcs.branch(b, "quote_line")
            lines = [eng.price(rfq, m, evidence.get(m.sku) if m.sku else None, customer, s, header.currency,
                               adj.get(m.sku or "", 0.0)) for m in matched]
            if lines:
                self.db.executemany(f"INSERT INTO {b} ({', '.join(QL_COLS)}) VALUES ({ph})",
                                    [[getattr(l, c) for c in QL_COLS] for l in lines])
            d = self.svc.vcs.diff(b, "quote_line")
            branches[s], quotes[s] = b, [l.model_dump() for l in lines]
            diffs[s] = {**asdict(d.branch), "total": d.branch.total, "mainline_changed": d.target.total}
            summary[s] = summarize(quotes[s])

        recommended = max(STRATEGIES, key=lambda s: summary[s]["expected_profit"])
        reason = "期望毛利（赢单概率 × 毛利额）最高"
        for m in (x for x in mems if x.memory_type == "profile"):
            hit = next((s for rx, s in _HINTS if rx.search(m.content)), None)
            if hit:
                recommended, reason = hit, f"客户画像记忆：{m.content[:60]}"
                break
        add_evidence(price_evidence={k: v.model_dump() for k, v in evidence.items()}, memory_adjust=adj,
                     memories=[_mem(m) for m in mems], branches=branches, recommended=recommended, reason=reason)
        self._status(rfq, "pricing")
        return {"evidence": {k: v.model_dump() for k, v in evidence.items()}, "branches": branches, "quotes": quotes,
                "branch_diffs": diffs, "summary": summary, "recommended": recommended, "recommend_reason": reason,
                "memories": (st.get("memories") or []) + [_mem(m) for m in mems]}

    # ---------- 5. 风险审核 ----------
    def risk(self, st: dict) -> dict:
        header = RFQHeader(**st["header"])
        rules = Rules(self.db)
        matched = [MatchedItem(**m) for m in st.get("matched", [])]
        risks = {s: [r.model_dump() for r in assess(header, matched, [QuoteLine(**l) for l in lines],
                                                   st.get("evidence", {}), rules, st.get("parse_notes", []))]
                 for s, lines in (st.get("quotes") or {}).items()}
        gate = st.get("gate") or {}
        if gate.get("injection"):
            hit = next(v for v in gate["verdicts"] if v["claim"] in ("injection", "hidden_text"))
            msg = (f"询价含提示注入/隐藏文字（{hit['source']}：{hit['reasons'][0]}），已被 MemoryGate 拦截、"
                   "不会进入记忆；价格由规则引擎计算，请核实发件方")
            for v in risks.values():
                v.append(RiskItem(level="high", code="prompt_injection", message=msg).model_dump())
        if gate.get("review"):
            for v in risks.values():
                v.append(RiskItem(level="low", code="memory_review",
                                  message=f"{gate['review']} 条客户声明待评审（记忆分支 {gate['inbox']}）").model_dump())
        add_evidence(risk_count={s: dict(Counter(r["level"] for r in v)) for s, v in risks.items()})
        self._status(st["rfq_id"], "pending_approval")
        return {"risks": risks, "status": "pending_approval"}

    # ---------- 6. 人工审批（LangGraph interrupt，恢复时携带审批决定） ----------
    def approval(self, st: dict) -> dict:
        raw = interrupt({
            "rfq_id": st["rfq_id"], "recommended": st.get("recommended"), "reason": st.get("recommend_reason"),
            "summary": st.get("summary"),
            "branch_diffs": st.get("branch_diffs"), "branches": st.get("branches"),
            "high_risks": {s: sum(r["level"] == "high" for r in v) for s, v in (st.get("risks") or {}).items()}})
        dec = ApprovalDecision.model_validate(raw or {})
        if dec.strategy not in (st.get("branches") or {}):
            raise ValueError(f"未知策略: {dec.strategy}")
        set_human(dec.action, dec.model_dump())
        return {"decision": dec.model_dump(), "status": "approved" if dec.action == "approve" else "rejected"}

    # ---------- 7. 提交：人工改价写入分支 → MERGE 整单 / PICK 部分行 → 清理分支 → 沉淀记忆 ----------
    def commit(self, st: dict) -> dict:
        rfq = st["rfq_id"]
        dec = ApprovalDecision(**st["decision"])
        b = st["branches"][dec.strategy]
        lines = {l["line_key"]: l for l in st["quotes"][dec.strategy]}
        changes = []
        for key, new in dec.overrides.items():
            ln = lines.get(key)
            if not ln or new <= 0:
                continue
            note = f"{ln['rationale']}；人工改价 {ln['unit_price']} → {new}（{dec.approver}）"
            self.db.execute(f"UPDATE {b} SET unit_price=%s, margin=%s, rationale=%s WHERE line_key=%s",
                            (new, round((new - ln["unit_cost"]) / new, 4), note, key))
            changes.append({"line_key": key, "sku": ln["sku"], "before": ln["unit_price"], "after": new,
                            "pct": round(new / ln["unit_price"] - 1, 4) if ln["unit_price"] else None})
        # 未定价（unit_price=0 且未人工改价）的行不进入主线
        valid = [k for k, l in lines.items() if l["unit_price"] > 0 or any(c["line_key"] == k for c in changes)]
        keys = [k for k in (dec.line_keys if dec.line_keys is not None else valid) if k in lines]
        diff = self.svc.vcs.diff(b, "quote_line")
        if keys and set(keys) == set(lines):
            self.svc.vcs.merge(b, "quote_line", "ACCEPT")
            op = "merge"
        else:
            self.svc.vcs.pick(b, "quote_line", keys, "ACCEPT")
            op = "pick"
        for name in st["branches"].values():
            self.svc.vcs.drop_branch(name)
        committed = self.db.query("SELECT * FROM quote_line WHERE rfq_id=%s ORDER BY line_no", (rfq,))
        self._status(rfq, "quoted")

        # 记忆沉淀（经 MemoryGate）：单据摘要（情节记忆）+ 人工改价经验（程序性记忆，下次定价自动参考；幅度异常的先复核）
        customer = st.get("customer") or {}
        cid, cname = customer.get("customer_id"), customer.get("name") or st["header"].get("customer_name") or "未知客户"
        summ = summarize(committed) if committed else {"amount": 0, "margin": 0}
        cur = st["header"].get("currency")
        gate = MemoryGate(self.svc)
        writes = [gate.write_internal(
            rfq, f"RFQ {rfq} 客户 {cname} 报价 {len(committed)} 行，策略 {STRATEGY_LABEL[dec.strategy]}，"
                 f"金额 {cur} {summ['amount']:,.2f}，毛利 {summ['margin']:.1%}", "episodic", cid, "T2", f"rfq:{rfq}")]
        for c in changes:
            if c["pct"] is not None and c["sku"]:
                writes.append(gate.write_internal(
                    rfq, f"人工改价经验 sku={c['sku']} adj={c['pct']:.4f}：客户 {cname} 的 {c['sku']} 原报 {c['before']} "
                         f"改为 {c['after']}（{dec.comment or '无备注'}）", "procedural", cid, "T2", f"override:{rfq}"))
        set_human(f"commit:{op}", {"strategy": dec.strategy, "keys": keys, "overrides": changes,
                                   "approver": dec.approver, "comment": dec.comment})
        add_evidence(branch=b, op=op, branch_diff=asdict(diff.branch), committed_lines=len(committed),
                     memory_writes=[w.brief() for w in writes])
        return {"committed": committed, "status": "quoted"}

    # ---------- 8. 输出报价单与回复邮件 ----------
    def render(self, st: dict) -> dict:
        rfq, header, customer = st["rfq_id"], st["header"], st.get("customer")
        lines = st.get("committed") or []
        s = self.svc.settings
        out = s.path(f"{s.output_dir}/{rfq}/{rfq}.xlsx").parent
        files = {"xlsx": str(render_excel(out / f"{rfq}.xlsx", rfq, header, customer, lines)),
                 "pdf": str(render_pdf(out / f"{rfq}.pdf", rfq, header, customer, lines)),
                 "html": str(render_html(out / f"{rfq}.html", rfq, header, customer, lines))}
        brief = {"rfq_id": rfq, "customer": (customer or {}).get("name") or header.get("customer_name"),
                 "terms": {k: header.get(k) for k in ("currency", "incoterm", "destination", "delivery_deadline",
                                                      "payment_terms")},
                 "lines": [{k: l[k] for k in ("line_no", "sku", "description", "qty", "unit", "unit_price")}
                           for l in lines]}
        set_prompt(PROMPT_VER["reply_email"])
        r = self.svc.router.run("reply_email", ReplyDraft, REPLY_SYSTEM, json.dumps(brief, ensure_ascii=False),
                                rule_fn=lambda: reply_rule(rfq, header, customer, lines))
        reply = r.output.model_dump()
        (out / "reply.txt").write_text(f"Subject: {reply['subject']}\n\n{reply['body']}", encoding="utf-8")
        files["reply"] = str(out / "reply.txt")
        add_evidence(files=files)
        return {"outputs": {"files": files, "reply": reply}, "status": "quoted"}

    # ---------- 驳回：丢弃全部策略分支，主线不受影响 ----------
    def reject(self, st: dict) -> dict:
        for name in (st.get("branches") or {}).values():
            self.svc.vcs.drop_branch(name)
        self._status(st["rfq_id"], "rejected")
        return {"status": "rejected"}
