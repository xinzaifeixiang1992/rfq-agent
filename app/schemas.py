"""跨节点共享的结构化数据契约。所有 Agent 节点的输入输出都以此为准。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Strategy = Literal["win_first", "balanced", "margin_first"]
STRATEGIES: tuple[Strategy, ...] = ("win_first", "balanced", "margin_first")
STRATEGY_LABEL = {"win_first": "争单优先", "balanced": "均衡", "margin_first": "利润优先"}


class RFQHeader(BaseModel):
    intent: Literal["new_rfq", "revision", "follow_up", "other"] = "new_rfq"
    customer_name: str | None = None
    sender_email: str | None = None
    currency: str | None = None
    incoterm: str | None = None
    destination: str | None = None
    delivery_deadline: str | None = None
    payment_terms: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class LineItem(BaseModel):
    line_no: int
    part_no: str | None = None
    description: str = ""
    qty: float = 0
    unit: str | None = None
    target_price: float | None = None
    source: str = ""  # 来源文件/页码，便于审计追溯
    confidence: float = 1.0


class LineItemList(BaseModel):
    items: list[LineItem] = Field(default_factory=list)
    confidence: float = 0.0


class MatchedItem(LineItem):
    sku: str | None = None
    sku_name: str | None = None
    std_unit: str | None = None
    match_method: str = ""  # exact | alias | memory | fuzzy | none
    match_score: float = 0.0


class PriceEvidence(BaseModel):
    sku: str
    unit_cost_cny: float
    last_quote_price: float | None = None
    last_won_price: float | None = None
    avg_won_price: float | None = None
    trend_pct: float = 0.0  # 近期报价趋势（相对早期均价）
    history_ids: list[str] = Field(default_factory=list)


class QuoteLine(BaseModel):
    line_key: str
    rfq_id: str
    line_no: int
    sku: str | None
    description: str
    qty: float
    unit: str | None
    unit_cost: float
    unit_price: float
    currency: str
    margin: float
    strategy: str
    win_prob: float = 0.0
    rationale: str = ""


class RiskItem(BaseModel):
    level: Literal["high", "medium", "low"]
    code: str
    message: str
    line_key: str | None = None


class ApprovalDecision(BaseModel):
    action: Literal["approve", "reject"] = "approve"
    strategy: Strategy = "balanced"
    line_keys: list[str] | None = None  # None=整单合并；否则只 PICK 指定行
    overrides: dict[str, float] = Field(default_factory=dict)  # line_key -> 人工改价
    approver: str = "sales"
    comment: str = ""
