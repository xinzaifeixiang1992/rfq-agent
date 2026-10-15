from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.db.conn import Database

_current: ContextVar["AuditCollector | None"] = ContextVar("audit_collector", default=None)


@dataclass
class AuditCollector:
    node: str
    models: list[str] = field(default_factory=list)
    tokens: int = 0
    cost: float = 0.0
    confidence: float | None = None
    evidence: dict = field(default_factory=dict)
    prompt_ver: str = ""
    human_action: str | None = None
    override: Any = None

    def add_call(self, model: str, tokens: int, cost: float, ok: bool) -> None:
        self.models.append(model if ok else f"{model}(x)")
        self.tokens += tokens
        self.cost += cost


def current() -> AuditCollector | None:
    return _current.get()


def add_evidence(**kw: Any) -> None:
    c = current()
    if c:
        c.evidence.update(kw)


def set_prompt(ver: str) -> None:
    c = current()
    if c and ver not in c.prompt_ver.split(","):
        c.prompt_ver = ",".join(filter(None, [c.prompt_ver, ver]))


def set_human(action: str, override: Any = None) -> None:
    c = current()
    if c:
        c.human_action, c.override = action, override


def set_confidence(v: float) -> None:
    c = current()
    if c:
        c.confidence = v if c.confidence is None else min(c.confidence, v)


def _dumps(obj: Any, limit: int = 20000) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


class AuditLogger:
    def __init__(self, db: Database, rule_version: Callable[[], str]) -> None:
        self.db = db
        self.rule_version = rule_version

    def write(self, rfq_id: str, node: str, *, output: Any, collector: AuditCollector | None = None,
              latency_ms: int = 0, data_snapshot: str = "", memory_snapshot: str = "",
              input_digest: str = "", human_action: str | None = None, override: Any = None) -> str:
        c = collector or AuditCollector(node=node)
        human_action = human_action or c.human_action
        override = override if override is not None else c.override
        did = f"D{time.time_ns():x}{uuid.uuid4().hex[:4]}"  # 按时间可排序
        self.db.execute(
            "INSERT INTO decision_log VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (did, rfq_id, node, " > ".join(c.models) or "rule-engine", c.prompt_ver, self.rule_version(),
             data_snapshot, memory_snapshot, input_digest, _dumps(c.evidence), _dumps(output),
             c.confidence, latency_ms, c.tokens, round(c.cost, 6), human_action,
             _dumps(override) if override is not None else None, datetime.now().isoformat(timespec="seconds")))
        return did

    def wrap(self, node: str, fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
        """包装 LangGraph 节点：自动采集模型调用、证据、耗时并写审计。"""

        def _node(state: dict) -> dict:
            col = AuditCollector(node=node)
            token = _current.set(col)
            t0 = time.perf_counter()
            try:
                out = fn(state)
            finally:
                _current.reset(token)
            self.write(state.get("rfq_id", ""), node, output=out, collector=col,
                       latency_ms=int((time.perf_counter() - t0) * 1000),
                       data_snapshot=state.get("data_snapshot", "") or out.get("data_snapshot", ""),
                       memory_snapshot=state.get("memory_snapshot", "") or out.get("memory_snapshot", ""),
                       input_digest=digest({k: v for k, v in state.items() if k != "trace"}))
            return out

        _node.__name__ = node
        return _node

    def list(self, rfq_id: str) -> list[dict]:
        rows = self.db.query("SELECT * FROM decision_log WHERE rfq_id=%s ORDER BY decision_id", (rfq_id,))
        for r in rows:
            for k in ("evidence_json", "output_json", "override_json"):
                if r.get(k):
                    try:
                        r[k] = json.loads(r[k])
                    except json.JSONDecodeError:
                        pass
        return rows
