"""基于业务库的本地记忆实现（开发/离线用），语义与 Memoria 的快照/分支/合并/回滚保持一致。"""
from __future__ import annotations

import uuid
from datetime import datetime

from app.db.conn import Database
from app.memory.base import MemoryItem, MemoryStore, rank

_COLS = "memory_id, memory_type, content, customer_id, trust_tier, source, created_at"


class LocalMemoryStore(MemoryStore):
    backend = "local"

    def __init__(self, db: Database) -> None:
        self.db = db
        self.current_branch = "main"

    def _item(self, r: dict, score: float = 0.0) -> MemoryItem:
        return MemoryItem(memory_id=r["memory_id"], content=r["content"], memory_type=r["memory_type"],
                          customer_id=r["customer_id"], trust_tier=r["trust_tier"], source=r["source"],
                          created_at=r["created_at"], score=score)

    def store(self, content, kind="semantic", customer_id=None, trust_tier="T3", source=""):
        mid = f"M{uuid.uuid4().hex[:12]}"
        self.db.execute(f"INSERT INTO memory_item (branch, {_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (self.current_branch, mid, kind, content, customer_id, trust_tier, source,
                         datetime.now().isoformat(timespec="seconds")))
        return mid

    def list(self, limit=100):
        rows = self.db.query(f"SELECT {_COLS} FROM memory_item WHERE branch=%s ORDER BY created_at DESC LIMIT {int(limit)}",
                             (self.current_branch,))
        return [self._item(r) for r in rows]

    def retrieve(self, query, customer_id=None, top_k=5, kinds=None):
        return rank(self._filter(self.list(1000), customer_id, kinds), query, customer_id, top_k)

    def forget(self, memory_id, reason=""):
        self.db.execute("DELETE FROM memory_item WHERE branch=%s AND memory_id=%s", (self.current_branch, memory_id))

    def snapshot(self, name, description=""):
        self.db.execute("DELETE FROM memory_snapshot WHERE snapshot_name=%s", (name,))
        self.db.execute(f"INSERT INTO memory_snapshot (snapshot_name, {_COLS}) "
                        f"SELECT %s, {_COLS} FROM memory_item WHERE branch='main'", (name,))
        # 空记忆空间也要能留下快照标记
        self.db.execute(f"INSERT INTO memory_snapshot (snapshot_name, {_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (name, "__marker__", "marker", description, None, "", "", datetime.now().isoformat()))
        return name

    def snapshots(self):
        return self.db.query("SELECT snapshot_name AS name, content AS description, created_at FROM memory_snapshot "
                             "WHERE memory_id='__marker__' ORDER BY created_at DESC")

    def rollback(self, name):
        if not self.db.query_one("SELECT 1 AS x FROM memory_snapshot WHERE snapshot_name=%s", (name,)):
            raise ValueError(f"记忆快照不存在: {name}")
        with self.db._lock:
            self.db.execute("DELETE FROM memory_item WHERE branch='main'")
            self.db.execute(f"INSERT INTO memory_item (branch, {_COLS}) SELECT 'main', {_COLS} FROM memory_snapshot "
                            "WHERE snapshot_name=%s AND memory_id<>'__marker__'", (name,))

    def branch(self, name):
        self.delete_branch(name)
        self.db.execute(f"INSERT INTO memory_item (branch, {_COLS}) SELECT %s, {_COLS} FROM memory_item "
                        "WHERE branch='main'", (name,))

    def checkout(self, name):
        self.current_branch = name

    def diff(self, name):
        main = {r["memory_id"]: r for r in self.db.query(f"SELECT {_COLS} FROM memory_item WHERE branch='main'")}
        out = []
        for r in self.db.query(f"SELECT {_COLS} FROM memory_item WHERE branch=%s", (name,)):
            if r["memory_id"] not in main:
                out.append({"op": "add", **r})
            elif main[r["memory_id"]]["content"] != r["content"]:
                out.append({"op": "update", **r})
        return out

    def merge(self, name):
        with self.db._lock:
            for r in self.diff(name):
                self.db.execute("DELETE FROM memory_item WHERE branch='main' AND memory_id=%s", (r["memory_id"],))
                self.db.execute(f"INSERT INTO memory_item (branch, {_COLS}) VALUES ('main',%s,%s,%s,%s,%s,%s,%s)",
                                [r[c.strip()] for c in _COLS.split(",")])

    def delete_branch(self, name):
        if name == "main":
            raise ValueError("不能删除 main 分支")
        self.db.execute("DELETE FROM memory_item WHERE branch=%s", (name,))
        if self.current_branch == name:
            self.current_branch = "main"
