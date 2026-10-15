"""图状态。全部为可 JSON 序列化的普通结构，便于 checkpoint 与审计落库。"""
from __future__ import annotations

from typing import TypedDict


class RFQState(TypedDict, total=False):
    rfq_id: str
    subject: str
    sender: str
    body: str
    attachments: list[str]
    work_dir: str
    customer: dict | None
    header: dict  # RFQHeader
    clarification: str | None  # 缺字段时的追问邮件草稿
    items: list[dict]  # LineItem
    parse_notes: list[str]
    complexity: str
    attachment_texts: list[dict]  # [{file, text, hidden}] 附件全文与隐藏文字（供 MemoryGate）
    gate: dict  # MemoryGate 判定：inbox 分支、拦截/待评审计数、逐条 verdict
    matched: list[dict]  # MatchedItem
    evidence: dict[str, dict]  # sku -> PriceEvidence
    memories: list[dict]  # 本单引用到的记忆（审计用）
    branches: dict[str, str]  # strategy -> 数据分支表名
    quotes: dict[str, list[dict]]  # strategy -> QuoteLine 列表
    branch_diffs: dict[str, dict]  # strategy -> DIFF 摘要
    summary: dict[str, dict]  # strategy -> 金额/毛利/期望利润
    recommended: str
    recommend_reason: str
    risks: dict[str, list[dict]]  # strategy -> RiskItem 列表
    decision: dict  # ApprovalDecision
    committed: list[dict]
    outputs: dict
    status: str
    data_snapshot: str
    memory_snapshot: str
