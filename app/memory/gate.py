"""MemoryGate：分级信任的记忆写入网关。原则——外部内容永远不能直接进入记忆主线。

外部（邮件正文、附件可见/隐藏文字）：
  抽取长期声明候选 → 三道确定性校验（注入特征 / 敏感字段需内部证据 / 与主数据·主线记忆矛盾）
  → block：不落记忆，只进评审表与审计；skip：与主数据一致、无需记忆；
    review：写入隔离分支 inbox_<rfq>（T4），人工评审后升级为 T3、先打 pre_merge 快照再合并。
内部（审批改价、成交反馈、手工录入）：注入检查；改价幅度超过阈值的经验转人工评审。
事后：scan() 复查主线（注入残留、无审计出处的改价经验、与主数据矛盾），隔离并列出受影响报价单。
所有阈值来自 pricing_rule（gate_*），可在规则沙盒中试验。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.memory.base import MemoryItem
from app.pricing.rules import Rules

INJECTION = [
    (re.compile(r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions?|rules|prompts?)", re.I),
     "忽略先前指令"),
    (re.compile(r"\b(note|message|instructions?)\s+(to|for)\s+(the\s+)?(ai|assistant|model|agent|llm|bot)\b", re.I),
     "对 AI 喊话"),
    (re.compile(r"\b(ai|assistant|agent|llm|chatgpt)\s*[:,，]\s*(please\s+)?(remember|note|always|store|save|apply)",
                re.I), "对 AI 下指令"),
    (re.compile(r"(^|\s)(system|assistant)\s*[:：]\s|<\|im_start\|>|\[/?INST\]|\[system\]", re.I), "伪造角色标记"),
    (re.compile(r"\b(remember|memori[sz]e)\b.{0,40}\b(future|subsequent|from now on|going forward)\b", re.I),
     "要求写入长期记忆"),
    (re.compile(r"\b(disregard|override|bypass)\b.{0,30}\b(rules?|polic(y|ies)|guardrails?|approval|instructions?)",
                re.I), "要求绕过规则"),
    (re.compile(r"\b(quote|price)\s+(everything|all(\s+items)?)\s+at\s+(cost|zero|0)\b", re.I), "要求按成本报价"),
    (re.compile(r"忽略.{0,6}(之前|以上|前面|先前|所有).{0,6}(指令|规则|设定|提示)"), "忽略先前指令"),
    (re.compile(r"(致|给|告诉).{0,4}(AI|ai|模型|助手|智能体)"), "对 AI 喊话"),
    (re.compile(r"(记住|牢记|写入记忆).{0,30}(以后|今后|所有|每次)"), "要求写入长期记忆"),
    (re.compile(r"(以后|今后)(所有|每次|一律).{0,10}报价"), "篡改长期报价策略"),
]
ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
PERSIST = re.compile(r"standing|always|all (future )?(orders|quotes|quotations|purchases|rfqs)|going forward|"
                     r"from now on|framework|annual|long[- ]term|as agreed|agreement|contract|"
                     r"长期|今后|以后|所有订单|框架|协议|合同|约定|一直|每次", re.I)
SKU = re.compile(r"\b([A-Z]{3}-\d{4})\b")
CLAIMS = {
    "discount": [re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:discount|off|rebate|reduction)", re.I),
                 re.compile(r"(?:discount|rebate|折扣|返点|让利|优惠)\D{0,25}?(\d{1,2}(?:\.\d+)?)\s*%", re.I),
                 re.compile(r"打\s*([1-9](?:\.\d)?)\s*折")],
    "payment_terms": [re.compile(r"\bnet\s*(\d{2,3})\b", re.I),
                      re.compile(r"(\d{2,3})\s*days?\s*(?:payment|credit|terms)", re.I),
                      re.compile(r"账期\s*(\d{2,3})\s*天")],
    "price": [re.compile(r"(?:agreed|contract|framework|special|fixed|协议|约定|合同|固定)\s*(?:unit\s*)?"
                         r"(?:price|价格?)\D{0,30}?(\d+(?:\.\d+)?)", re.I)],
    "preference": [re.compile(r"(?:prefer|require|偏好|要求|看重)\s*(.{3,80})", re.I)],
}
ALIAS = re.compile(r"\b([A-Z0-9][A-Z0-9\-]{3,})\s*(?:=|is the same as|same as|equivalent to|equals|对应|即为|就是)\s*"
                   r"(?:our\s+|your\s+)?(?:SKU\s*)?([A-Z]{3}-\d{4})\b", re.I)
_ADJ = re.compile(r"sku=(\S+)\s+adj=([-+]?\d*\.?\d+)")
_OPPOSITE = ((re.compile(r"价格敏感|比价|price[- ]sensitive", re.I), re.compile(r"不敏感|看重(质量|交期)|quality first", re.I)),)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Candidate:
    content: str
    claim: str  # injection | hidden_text | discount | payment_terms | price | alias | preference | override | note
    source: str
    customer_id: str | None = None
    kind: str = "profile"
    value: str | float | None = None
    sku: str | None = None
    hidden: bool = False
    injection: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    decision: str  # block | review | skip | accept
    risk: str  # high | medium | low
    reasons: list[str]
    candidate: Candidate
    memory_id: str | None = None
    review_id: str | None = None

    def brief(self) -> dict:
        c = self.candidate
        return {"decision": self.decision, "risk": self.risk, "claim": c.claim, "source": c.source,
                "content": c.content[:200], "reasons": self.reasons, "memory_id": self.memory_id,
                "review_id": self.review_id}


def injection_marks(text: str) -> list[str]:
    marks = [label for rx, label in INJECTION if rx.search(text)]
    if ZERO_WIDTH.search(text):
        marks.append("零宽字符")
    return list(dict.fromkeys(marks))


def _sentences(text: str, join: bool) -> list[str]:
    blocks = [re.sub(r"\s*\n\s*", " ", p) for p in re.split(r"\n\s*\n", text)] if join else text.splitlines()
    out = []
    for b in blocks:
        for s in re.split(r"(?<=[。！？!?；;])|(?<=\.)\s+(?=[A-Z\u4e00-\u9fff])", b):
            if len(s.strip()) >= 6:
                out.append(s.strip())
    return out


def _days(terms: str | None) -> int | None:
    if terms is None:
        return None
    m = re.search(r"(\d{2,3})\s*days|net\s*(\d{2,3})", terms, re.I)
    return int(m.group(1) or m.group(2)) if m else 0  # T/T in advance、L/C at sight、预付款 = 0 天账期


def extract(text: str, source: str, customer_id: str | None, hidden: bool = False) -> list[Candidate]:
    """确定性抽取：每句至多产出一个候选。注入/隐藏文字优先；业务声明须带长期性措辞（别名声明除外）。"""
    out = []
    for s in _sentences(text, join=hidden):
        marks = injection_marks(s)
        if marks:
            out.append(Candidate(s, "injection", source, customer_id, hidden=hidden, injection=marks))
            continue
        if hidden:
            out.append(Candidate(s, "hidden_text", source, customer_id, hidden=True))
            continue
        if (m := ALIAS.search(s)):
            out.append(Candidate(f"{m.group(1)} = SKU {m.group(2).upper()}", "alias", source, customer_id,
                                 kind="alias", value=m.group(1), sku=m.group(2).upper()))
            continue
        if not PERSIST.search(s):
            continue
        sku = SKU.search(s)
        plain = SKU.sub("<sku>", s)  # 先遮蔽料号，避免把料号里的数字当成价格/天数
        for claim, rxs in CLAIMS.items():
            m = next((x for x in (rx.search(plain) for rx in rxs) if x), None)
            if m:
                v = m.group(1)
                if claim == "discount":
                    v = round(1 - float(v) / 10, 4) if "折" in m.group(0) else float(v) / 100
                elif claim in ("payment_terms", "price"):
                    v = float(v)
                out.append(Candidate(s, claim, source, customer_id, value=v, sku=sku.group(1) if sku else None))
                break
    return out


class MemoryGate:
    def __init__(self, svc) -> None:
        self.svc, self.db, self.mem = svc, svc.db, svc.memory
        self.rules = Rules(self.db)

    # ---------- 校验 ----------
    def evaluate(self, c: Candidate) -> Verdict:
        if c.claim == "injection":
            return Verdict("block", "high", [f"提示注入特征：{'、'.join(c.injection)}"]
                           + (["出现在附件隐藏文字中"] if c.hidden else []), c)
        if c.claim == "hidden_text":
            return Verdict("block", "high", ["附件隐藏文字（白色/极小字号），人眼不可见的内容不得进入记忆"], c)
        cust = self.db.query_one("SELECT * FROM customer WHERE customer_id=%s", (c.customer_id,)) \
            if c.customer_id else None
        if c.claim in ("discount", "payment_terms", "price") and not cust:
            return Verdict("block", "high", ["敏感字段声明来自未识别客户，无内部证据可核对"], c)
        return getattr(self, f"_check_{c.claim}")(c, cust)

    def _check_discount(self, c, cust):
        r = self.db.query_one("SELECT AVG(1 - deal_price / unit_price) AS d, COUNT(*) AS n FROM quote_history "
                              "WHERE customer_id=%s AND won=1 AND deal_price IS NOT NULL AND unit_price > 0",
                              (cust["customer_id"],)) or {}
        hist, tol = float(r.get("d") or 0), self.rules.get("gate_discount_tol", 0.05)
        ev = f"历史成交平均让价 {hist:.1%}（{r.get('n') or 0} 单）"
        if c.value > hist + tol:
            return Verdict("block", "high", [f"折扣声明 {c.value:.0%} 无内部依据：{ev}，容差 {tol:.0%}"], c)
        return Verdict("review", "high", [f"敏感字段（折扣 {c.value:.1%}）需销售确认；{ev}"], c)

    def _check_payment_terms(self, c, cust):
        internal = _days(cust.get("payment_terms"))
        if int(c.value) == internal:
            return Verdict("skip", "low", [f"与客户主数据一致（{cust.get('payment_terms')}），无需记忆"], c)
        if c.value > internal:
            return Verdict("block", "high", [f"账期声明 {int(c.value)} 天与客户主数据 “{cust.get('payment_terms')}”"
                                             f"（{internal} 天）矛盾，且对我方不利"], c)
        return Verdict("review", "medium", [f"账期声明 {int(c.value)} 天短于主数据 {internal} 天，需财务确认"], c)

    def _check_price(self, c, cust):
        if not c.sku:
            return Verdict("block", "high", ["价格声明未指明物料，无法核对"], c)
        r = self.db.query_one("SELECT MIN(unit_price) AS lo, COUNT(*) AS n FROM quote_history "
                              "WHERE customer_id=%s AND sku=%s", (cust["customer_id"], c.sku)) or {}
        floor = self.rules.get("gate_price_floor", 0.8)
        if r.get("lo") and c.value < float(r["lo"]) * floor:
            return Verdict("block", "high", [f"{c.sku} 声明价 {c.value} 低于该客户历史最低报价 {r['lo']} 的 "
                                             f"{floor:.0%}"], c)
        # 无该客户历史时退回成本价：低于成本的"协议价"一律拦截（币种取声明中的币种，缺省 USD）
        cur = next((x for x in ("EUR", "GBP", "JPY", "HKD", "CNY", "RMB") if x in c.content.upper()), "USD")
        cost = self.db.query_one("SELECT p.unit_cost * f.rate_to_cny AS cny FROM cost_price p JOIN fx_rate f "
                                 "ON f.currency = p.currency WHERE p.sku=%s", (c.sku,))
        fx = self.db.query_one("SELECT rate_to_cny FROM fx_rate WHERE currency=%s",
                               ("CNY" if cur == "RMB" else cur,))
        if cost and fx and c.value * float(fx["rate_to_cny"]) < float(cost["cny"]):
            return Verdict("block", "high", [f"{c.sku} 声明价 {c.value} {cur} 低于我方成本"], c)
        return Verdict("review", "high", [f"{c.sku} 价格声明需销售确认（历史报价 {r.get('n') or 0} 条）"], c)

    def _check_alias(self, c, _cust):
        sku = self.db.query_one("SELECT sku, aliases FROM product_sku WHERE sku=%s", (c.sku,))
        if not sku:
            return Verdict("block", "high", [f"目标 SKU {c.sku} 不存在"], c)
        from app.pricing.matcher import SkuMatcher, norm_code
        code = norm_code(str(c.value))
        m = SkuMatcher(self.db)
        hit = m.by_code.get(code) or m.by_alias.get(code)
        if hit:
            return (Verdict("skip", "low", ["物料主数据已有此映射"], c) if hit["sku"] == c.sku else
                    Verdict("block", "high", [f"与物料主数据矛盾：{c.value} 已对应 {hit['sku']}"], c))
        for it in self._main(kinds=("alias",), customer_id=c.customer_id):
            mm = re.match(r"(\S+)\s*=\s*SKU\s*([A-Z0-9\-]+)", it.content)
            if mm and norm_code(mm.group(1)) == code:
                return (Verdict("skip", "low", ["主线记忆已有此映射"], c) if mm.group(2) == c.sku else
                        Verdict("review", "high", [f"与主线记忆矛盾：{it.memory_id} 记为 {mm.group(2)}"], c))
        return Verdict("review", "low", [f"新料号映射，目标 {c.sku} 存在"], c)

    def _check_preference(self, c, _cust):
        for it in self._main(kinds=("profile",), customer_id=c.customer_id):
            for a, b in _OPPOSITE:
                if (a.search(c.content) and b.search(it.content)) or (b.search(c.content) and a.search(it.content)):
                    return Verdict("review", "high", [f"与主线画像矛盾：{it.memory_id}「{it.content[:40]}」"], c)
        return Verdict("review", "low", ["客户偏好声明，未发现矛盾"], c)

    def _main(self, kinds=(), customer_id=None) -> list[MemoryItem]:
        with self.mem.on_branch("main"):
            items = self.mem.list(1000)
        return [i for i in items if (not kinds or i.memory_type in kinds)
                and (not customer_id or not i.customer_id or i.customer_id == customer_id)]

    # ---------- 评审表 ----------
    def record(self, rfq_id: str, v: Verdict, branch: str | None, status: str) -> str:
        rid = f"G{time.time_ns():x}{uuid.uuid4().hex[:4]}"
        c = v.candidate
        self.db.execute("INSERT INTO memory_review VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (rid, rfq_id, branch, v.memory_id, v.decision, v.risk, c.claim, c.content[:2000],
                         c.source[:128], json.dumps(v.reasons, ensure_ascii=False), status, None, now(),
                         None if status == "pending" else now()))
        v.review_id = rid
        return rid

    # ---------- 外部输入 ----------
    def screen(self, rfq_id: str, customer_id: str | None, texts: list[tuple[str, str, bool]]) -> dict:
        """texts: [(来源, 文本, 是否隐藏文字)]。返回本单的拦截/待评审摘要（写入审计证据）。"""
        cands = [c for src, t, hid in texts if t and t.strip() for c in extract(t, src, customer_id, hid)]
        verdicts = [self.evaluate(c) for c in cands]
        inbox = f"inbox_{rfq_id.lower()}"
        review = [v for v in verdicts if v.decision == "review"]
        if review:
            self.mem.branch(inbox)
            with self.mem.on_branch(inbox):
                for v in review:
                    c = v.candidate
                    v.memory_id = self.mem.store(f"{c.content}（客户声明，{rfq_id}）", kind=c.kind,
                                                 customer_id=c.customer_id, trust_tier="T4", source=f"ext:{rfq_id}")
        for v in verdicts:
            self.record(rfq_id, v, inbox if v.decision == "review" else None,
                        {"review": "pending", "block": "blocked"}.get(v.decision, "skipped"))
        count = {d: sum(v.decision == d for v in verdicts) for d in ("block", "review", "skip")}
        return {"inbox": inbox if review else None, **count,
                "injection": any(v.candidate.claim in ("injection", "hidden_text") for v in verdicts),
                "verdicts": [v.brief() for v in verdicts]}

    # ---------- 内部写入 ----------
    def write_internal(self, rfq_id: str, content: str, kind: str, customer_id: str | None, trust_tier: str,
                       source: str) -> Verdict:
        c = Candidate(content, "override" if kind == "procedural" else "note", source, customer_id, kind=kind)
        marks = injection_marks(content)
        if marks:
            v = Verdict("block", "high", [f"提示注入特征：{'、'.join(marks)}"], c)
            self.record(rfq_id, v, None, "blocked")
            return v
        cap = self.rules.get("gate_override_cap", 0.15)
        big = [float(a) for _, a in _ADJ.findall(content) if abs(float(a)) > cap]
        if kind == "procedural" and big:
            inbox = f"inbox_{rfq_id.lower()}"
            if inbox not in self._branches():
                self.mem.branch(inbox)
            with self.mem.on_branch(inbox):
                mid = self.mem.store(content, kind=kind, customer_id=customer_id, trust_tier="T4", source=source)
            v = Verdict("review", "high", [f"改价幅度 {big[0]:+.1%} 超过阈值 ±{cap:.0%}，经验需复核后才影响后续定价"],
                        c, memory_id=mid)
            self.record(rfq_id, v, inbox, "pending")
            return v
        return Verdict("accept", "low", [], c, memory_id=self.mem.store(content, kind=kind, customer_id=customer_id,
                                                                        trust_tier=trust_tier, source=source))

    def _branches(self) -> set[str]:
        return {r["branch"] for r in self.db.query("SELECT DISTINCT branch FROM memory_review WHERE status='pending' "
                                                   "AND branch IS NOT NULL")}

    # ---------- 人工评审 ----------
    def queue(self, status: str | None = None) -> list[dict]:
        cond, params = ("WHERE status=%s", (status,)) if status else ("", ())
        rows = self.db.query(f"SELECT * FROM memory_review {cond} ORDER BY review_id DESC LIMIT 500", params)
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return rows

    def approve(self, branch: str, reviewer: str, exclude: list[str] = ()) -> dict:
        rows = self.db.query("SELECT * FROM memory_review WHERE branch=%s AND status='pending'", (branch,))
        if not rows:
            raise KeyError(branch)
        snap_name = f"pre_merge_{branch}"
        try:
            snap = self.mem.snapshot(snap_name, f"{branch} 合并前")
        except Exception as exc:
            if self.mem.backend == "memoria" and "already exists" in str(exc).lower():
                snap = snap_name  # 上次合并前已成功打快照，失败重试时复用
            else:
                raise
        with self.mem.on_branch(branch):
            items = {i.memory_id: i for i in self.mem.list(1000)}
        accepted, rejected = [], []
        for r in rows:
            it = items.get(r["memory_id"])
            if r["review_id"] in exclude or r["memory_id"] in exclude or not it:
                rejected.append(r)
            else:
                accepted.append((r, it))

        # Memoria 对含 DELETE+INSERT 的旧分支可能报 lineage metadata missing；
        # 从 main 新建纯追加晋级分支（T4 → T3），成功合并前保留原 inbox 便于重试。
        target = branch
        if self.mem.backend == "memoria":
            target = f"promote_{uuid.uuid4().hex[:8]}_{branch}"[:64]
            self.mem.branch(target)
        approved: list[tuple[dict, str]] = []
        with self.mem.on_branch(target):
            if target == branch:
                for _, it in accepted:
                    self.mem.forget(it.memory_id)
                for r in rejected:
                    if it := items.get(r["memory_id"]):
                        self.mem.forget(it.memory_id)
            for r, it in accepted:
                mid = self.mem.store(it.content, kind=it.memory_type, customer_id=it.customer_id,
                                     trust_tier="T3", source=it.source)
                approved.append((r, mid))
        fallback = None
        try:
            diff = self.mem.diff(target)
        except Exception as exc:
            # Memoria 云的底层 lineage 异常会让新分支 diff 超时或返回 legacy ALTER；
            # 已有 pre_merge 快照时降级为人工批准后直接写 main，仍可完整回滚与审计。
            message = str(exc).lower()
            if self.mem.backend != "memoria" or not any(x in message for x in ("timed out", "timeout", "lineage")):
                raise
            fallback = str(exc)
            approved = []
            with self.mem.on_branch("main"):
                for r, it in accepted:
                    mid = self.mem.store(it.content, kind=it.memory_type, customer_id=it.customer_id,
                                         trust_tier="T3", source=it.source)
                    approved.append((r, mid))
            diff = [{"op": "add", "memory_id": mid, "content": it.content,
                     "memory_type": it.memory_type, "customer_id": it.customer_id,
                     "trust_tier": "T3", "source": it.source}
                    for (_, it), (_, mid) in zip(accepted, approved)]
        else:
            self.mem.merge(target)

        # 只在云端合并或快照保护的降级写入成功后落评审状态，避免失败重试时丢失 pending 记录。
        for r, mid in approved:
            self._decide(r["review_id"], "approved", reviewer, mid)
        for r in rejected:
            self._decide(r["review_id"], "rejected", reviewer)
        cleanup_errors = []
        for name in dict.fromkeys((target, branch)):
            try:
                self.mem.delete_branch(name)
            except Exception as exc:  # 合并已成功，清理失败不应把接口变成 500
                cleanup_errors.append(f"{name}: {exc}")
        out = {"branch": branch, "snapshot": snap, "approved": [mid for _, mid in approved],
               "rejected": len(rejected), "diff": diff}
        if fallback:
            out["fallback"] = "snapshot_protected_main_write"
            out["fallback_reason"] = fallback[:300]
        if cleanup_errors:
            out["cleanup_errors"] = cleanup_errors
        self.svc.audit.write(rows[0]["rfq_id"] or "", "memory_review", output=out, human_action=f"approve:{reviewer}")
        return out

    def reject(self, branch: str, reviewer: str) -> dict:
        rows = self.db.query("SELECT review_id, rfq_id FROM memory_review WHERE branch=%s AND status='pending'",
                             (branch,))
        if not rows:
            raise KeyError(branch)
        self.mem.delete_branch(branch)
        for r in rows:
            self._decide(r["review_id"], "rejected", reviewer)
        out = {"branch": branch, "rejected": len(rows)}
        self.svc.audit.write(rows[0]["rfq_id"] or "", "memory_review", output=out, human_action=f"reject:{reviewer}")
        return out

    def _decide(self, review_id: str, status: str, reviewer: str, memory_id: str | None = None) -> None:
        self.db.execute("UPDATE memory_review SET status=%s, reviewer=%s, decided_at=%s"
                        + (", memory_id=%s" if memory_id else "") + " WHERE review_id=%s",
                        (status, reviewer, now(), *([memory_id] if memory_id else []), review_id))

    # ---------- 事后复查 / 影响分析 ----------
    def impact(self, memory_id: str) -> list[dict]:
        """哪些报价单的哪些决策引用过这条记忆（决策审计 evidence 中记录了 memory_id）。"""
        rows = self.db.query("SELECT d.rfq_id, d.node, d.decision_id, d.created_at, r.status, r.customer_id "
                             "FROM decision_log d LEFT JOIN rfq_record r ON r.rfq_id = d.rfq_id "
                             "WHERE d.rfq_id <> '' AND d.evidence_json LIKE %s ORDER BY d.decision_id",
                             (f"%{memory_id}%",))
        out: dict[str, dict] = {}
        for r in rows:
            o = out.setdefault(r["rfq_id"], {"rfq_id": r["rfq_id"], "customer_id": r["customer_id"],
                                             "status": r["status"], "nodes": [], "first_used": r["created_at"]})
            o["nodes"].append(r["node"])
        for o in out.values():
            o["committed_lines"] = (self.db.query_one("SELECT COUNT(*) AS n FROM quote_line WHERE rfq_id=%s",
                                                      (o["rfq_id"],)) or {}).get("n", 0)
        return list(out.values())

    def _provenance_ok(self, it: MemoryItem) -> bool:
        """T1/T2 改价经验必须能在决策审计中找到对应的人工改价（commit 节点 override 里有该 SKU）。"""
        m = re.match(r"override:(\S+)", it.source or "")
        sku = _ADJ.search(it.content)
        if not (m and sku):
            return False
        row = self.db.query_one("SELECT override_json FROM decision_log WHERE rfq_id=%s AND node='commit' "
                                "AND override_json LIKE %s", (m.group(1), f"%{sku.group(1)}%"))
        return bool(row)

    def scan(self, apply: bool = False, reviewer: str = "system") -> dict:
        findings = []
        for it in self._main():
            reasons = []
            if (marks := injection_marks(it.content)):
                reasons.append(f"主线记忆含提示注入特征：{'、'.join(marks)}")
            if it.memory_type == "procedural" and _ADJ.search(it.content) and it.trust_tier in ("T1", "T2") \
                    and not self._provenance_ok(it):
                reasons.append(f"无审计出处的改价经验（来源 {it.source or '未知'} 在决策日志中找不到对应人工改价）")
            if it.memory_type == "profile" and it.customer_id:
                for c in extract(it.content, it.source, it.customer_id):
                    if c.claim in ("discount", "payment_terms", "price"):
                        v = self.evaluate(c)
                        if v.decision == "block":
                            reasons += v.reasons
            if reasons:
                findings.append({"memory_id": it.memory_id, "kind": it.memory_type, "trust": it.trust_tier,
                                 "source": it.source, "content": it.content[:200], "reasons": reasons,
                                 "impact": self.impact(it.memory_id)})
        out = {"findings": findings, "applied": False}
        if apply and findings:
            snap = self.mem.snapshot(f"pre_quarantine_{datetime.now():%Y%m%d%H%M%S%f}", "隔离前")
            with self.mem.on_branch("main"):
                for f in findings:
                    self.mem.forget(f["memory_id"], reason="MemoryGate quarantine: " + "; ".join(f["reasons"])[:200])
                    c = Candidate(f["content"], "quarantine", f["source"] or "", kind=f["kind"])
                    v = Verdict("block", "high", f["reasons"], c, memory_id=f["memory_id"])
                    self.record("", v, None, "quarantined")
            out.update(applied=True, snapshot=snap)
            self.svc.audit.write("", "memory_scan", output=out, human_action=f"quarantine:{reviewer}")
        return out
