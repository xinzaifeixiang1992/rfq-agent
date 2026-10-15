"""模型路由器。路由表 model_route 存在数据库中，本身受 Git4Data 版本管理（配置即数据）。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from pydantic import BaseModel

from app.audit.hook import current, set_confidence
from app.db.conn import Database
from app.router.llm import LLMClient

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
RULE_ENGINE = "rule-engine"


@dataclass
class RouteContext:
    complexity: str = "normal"  # normal | high（跨页表格、扫描件、超长邮件）
    high_value: bool = False  # 战略客户 / 大金额
    images: list[str] = field(default_factory=list)


@dataclass
class RouteResult:
    output: BaseModel
    model: str
    attempts: list[dict]
    tokens: int = 0
    cost: float = 0.0


class ModelRouter:
    def __init__(self, db: Database, llm: LLMClient | None) -> None:
        self.db = db
        self.llm = llm

    def route(self, task: str) -> dict:
        return self.db.query_one("SELECT * FROM model_route WHERE task=%s", (task,)) or {
            "task": task, "primary_model": None, "escalate_model": None, "fallback_model": None,
            "confidence_threshold": 0.7, "version": "default"}

    def _price(self, model: str) -> float:
        row = self.db.query_one("SELECT cost_per_1k FROM model_catalog WHERE model=%s", (model,))
        return float(row["cost_per_1k"]) if row else 0.0

    def plan(self, task: str, ctx: RouteContext) -> list[str]:
        """决定尝试顺序：高复杂度/高价值直接用升级模型；否则主模型 → 升级 → 兜底。"""
        r = self.route(task)
        order = [r["escalate_model"], r["fallback_model"]] if (ctx.high_value or ctx.complexity == "high") \
            else [r["primary_model"], r["escalate_model"], r["fallback_model"]]
        seen, out = set(), []
        for m in order:
            if m and m not in seen and m != RULE_ENGINE:
                seen.add(m)
                out.append(m)
        return out if self.llm else []

    def run(self, task: str, schema: type[T], system: str, user: str,
            rule_fn: Callable[[], T], ctx: RouteContext | None = None) -> RouteResult:
        ctx = ctx or RouteContext()
        threshold = float(self.route(task)["confidence_threshold"] or 0.7)
        col = current()
        attempts: list[dict] = []
        best: tuple[float, T, str] | None = None
        total_tokens, total_cost = 0, 0.0

        for model in self.plan(task, ctx):
            try:
                data, tokens = self.llm.chat_json(model, system, user, ctx.images or None)
                out = schema.model_validate(data)
                conf = float(getattr(out, "confidence", 1.0) or 0.0)
                cost = tokens / 1000 * self._price(model)
                total_tokens, total_cost = total_tokens + tokens, total_cost + cost
                attempts.append({"model": model, "ok": True, "confidence": conf, "tokens": tokens})
                if col:
                    col.add_call(model, tokens, cost, True)
                if best is None or conf > best[0]:
                    best = (conf, out, model)
                if conf >= threshold:
                    break  # 达到置信度阈值，不再升级
            except Exception as e:  # noqa: BLE001 - 网络/格式/校验任何失败都降级到下一档
                log.warning("route %s model %s failed: %s", task, model, e)
                attempts.append({"model": model, "ok": False, "error": str(e)[:200]})
                if col:
                    col.add_call(model, 0, 0.0, False)

        if best is None:
            out = rule_fn()
            attempts.append({"model": RULE_ENGINE, "ok": True})
            if col:
                col.add_call(RULE_ENGINE, 0, 0.0, True)
            best = (float(getattr(out, "confidence", 0.6) or 0.6), out, RULE_ENGINE)

        set_confidence(best[0])
        return RouteResult(output=best[1], model=best[2], attempts=attempts, tokens=total_tokens, cost=total_cost)
