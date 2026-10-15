"""规则参数读取。所有定价参数都来自数据库，改规则 = 改数据（可分支试验、可 DIFF、可回滚）。"""
from __future__ import annotations

from app.db.conn import Database


class Rules:
    def __init__(self, db: Database | None = None, rows: list[dict] | None = None) -> None:
        """默认读当前规则表；传入 rows（如某个快照下的规则）则用于历史重放。"""
        rows = rows if rows is not None else db.query("SELECT rule_key, rule_value FROM pricing_rule")
        self._kv = {r["rule_key"]: r["rule_value"] for r in rows}

    def get(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self._kv.get(key, default))
        except (TypeError, ValueError):
            return default

    def text(self, key: str, default: str = "") -> str:
        return str(self._kv.get(key, default))

    @property
    def version(self) -> str:
        return self.text("version", "unversioned")

    def qty_factor(self, qty: float) -> float:
        """阶梯价，格式 "100:1.0,500:0.97,1000:0.95"。"""
        factor = 1.0
        for pair in self.text("qty_break", "").split(","):
            if ":" in pair:
                q, f = pair.split(":")
                if qty >= float(q):
                    factor = float(f)
        return factor


def rule_version(db: Database) -> str:
    row = db.query_one("SELECT rule_value FROM pricing_rule WHERE rule_key='version'")
    return row["rule_value"] if row else "unversioned"
