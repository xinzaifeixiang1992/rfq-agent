from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Sequence

from app.db.conn import Database

Conflict = Literal["FAIL", "SKIP", "ACCEPT"]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class VCSConflict(RuntimeError):
    pass


def ident(name: str) -> str:
    """校验标识符，防止拼接 SQL 时注入。"""
    if not _IDENT.match(name):
        raise ValueError(f"非法标识符: {name}")
    return name


@dataclass
class DiffSummary:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.deleted


@dataclass
class DiffResult:
    branch: DiffSummary = field(default_factory=DiffSummary)
    target: DiffSummary = field(default_factory=DiffSummary)
    rows: list[dict] = field(default_factory=list)


class DataVCS:
    """所有业务代码只通过此接口做版本操作，不直接拼 Git4Data SQL。"""

    backend: str = ""

    def __init__(self, db: Database) -> None:
        self.db = db

    def snapshot(self, name: str, table: str) -> str: ...
    def read_at(self, table: str, snapshot: str, where: str = "", params: Sequence = ()) -> list[dict]: ...
    def restore(self, table: str, snapshot: str) -> None: ...
    def branch(self, name: str, source: str) -> str: ...
    def diff(self, branch: str, against: str) -> DiffResult: ...
    def merge(self, branch: str, into: str, on_conflict: Conflict = "FAIL") -> None: ...
    def pick(self, branch: str, into: str, keys: Sequence[str], on_conflict: Conflict = "ACCEPT") -> None: ...
    def drop_branch(self, name: str) -> None: ...

    def compare(self, a: str, b: str, pk: str, cols: Sequence[str], where: str = "",
                params: Sequence = ()) -> list[dict]:
        """应用层逐行对比两张同构表（用于策略分支间的价格对比展示）。"""
        cond = f" WHERE {where}" if where else ""
        cl = ", ".join([pk, *cols])
        ra = {r[pk]: r for r in self.db.query(f"SELECT {cl} FROM {ident(a)}{cond}", params)}
        rb = {r[pk]: r for r in self.db.query(f"SELECT {cl} FROM {ident(b)}{cond}", params)}
        out = []
        for k in sorted(set(ra) | set(rb)):
            x, y = ra.get(k), rb.get(k)
            changed = [c for c in cols if (x or {}).get(c) != (y or {}).get(c)]
            if changed:
                out.append({"key": k, "changed": changed, a: x, b: y})
        return out
