"""记忆即数据：记忆主线 mem_main 本身是一张受 Git4Data 管理的表。

分支 = DATA BRANCH 表 mb_<name>，快照 = 表级 SNAPSHOT，回滚 = RESTORE TABLE，评审 = DIFF，入主线 = MERGE。
与报价/规则共用同一套 DataVCS：MatrixOne 上走原生语法，SQLite 上走仿真，语义一致。
"""
from __future__ import annotations

import re
import uuid
import zlib
from datetime import datetime

from app.db.conn import Database
from app.memory.base import MemoryItem, MemoryStore, rank
from app.vcs.base import DataVCS, ident

MAIN = "mem_main"
_COLS = ("memory_id", "memory_type", "content", "customer_id", "trust_tier", "source", "created_at")
_OPS = {"INSERT": "add", "UPDATE": "update", "DELETE": "delete"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "x"


class Git4DataMemoryStore(MemoryStore):
    def __init__(self, db: Database, vcs: DataVCS) -> None:
        self.db, self.vcs = db, vcs
        self.backend = "matrixone" if vcs.backend == "matrixone" else "git4data-sim"
        self.current_branch = "main"

    def _table(self, name: str | None = None) -> str:
        name = name or self.current_branch
        return MAIN if name == "main" else ident(f"mb_{_slug(name)}"[:64])

    def _vcs_snap(self, name: str) -> str:
        # MatrixOne 快照是账户级对象：名字带上库名与原名的校验码，避免不同库/相近名字互相覆盖
        tag = zlib.crc32(f"{getattr(self.db, 'database', 'local')}/{name}".encode())
        return ident(f"ms_{_slug(name)[:40]}_{tag:08x}")

    @staticmethod
    def _item(r: dict) -> MemoryItem:
        return MemoryItem(**{c: r[c] for c in _COLS})

    def store(self, content, kind="semantic", customer_id=None, trust_tier="T3", source=""):
        mid = f"M{uuid.uuid4().hex[:12]}"
        self.db.execute(f"INSERT INTO {self._table()} ({', '.join(_COLS)}) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (mid, kind, content, customer_id, trust_tier, source,
                         datetime.now().isoformat(timespec="seconds")))
        return mid

    def list(self, limit=100):
        rows = self.db.query(f"SELECT {', '.join(_COLS)} FROM {self._table()} ORDER BY created_at DESC "
                             f"LIMIT {int(limit)}")
        return [self._item(r) for r in rows]

    def retrieve(self, query, customer_id=None, top_k=5, kinds=None):
        return rank(self._filter(self.list(1000), customer_id, kinds), query, customer_id, top_k)

    def forget(self, memory_id, reason=""):
        self.db.execute(f"DELETE FROM {self._table()} WHERE memory_id=%s", (memory_id,))

    # ---------- 版本操作：全部委托 DataVCS ----------
    def snapshot(self, name, description=""):
        vn = self._vcs_snap(name)
        self.vcs.snapshot(vn, MAIN)
        self.db.execute("DELETE FROM mem_snapshot_meta WHERE name=%s", (name,))
        self.db.execute("INSERT INTO mem_snapshot_meta VALUES (%s,%s,%s,%s)",
                        (name, vn, description[:500], datetime.now().isoformat(timespec="seconds")))
        return name

    def snapshots(self):
        return self.db.query("SELECT name, description, created_at, vcs_name FROM mem_snapshot_meta "
                             "ORDER BY created_at DESC")

    def rollback(self, name):
        row = self.db.query_one("SELECT vcs_name FROM mem_snapshot_meta WHERE name=%s", (name,))
        if not row:
            raise ValueError(f"记忆快照不存在: {name}")
        self.vcs.restore(MAIN, row["vcs_name"])

    def branch(self, name):
        if name == "main":
            raise ValueError("main 不能作为分支名")
        self.vcs.drop_branch(self._table(name))
        self.vcs.branch(self._table(name), MAIN)

    def checkout(self, name):
        if name != "main" and not self.db.table_exists(self._table(name)):
            raise ValueError(f"记忆分支不存在: {name}")
        self.current_branch = name

    def diff(self, name):
        """分支相对主线的净变更（基于共同祖先；主线侧的变更不计入）。"""
        t = self._table(name)
        out = []
        for r in self.vcs.diff(t, MAIN).rows:
            if r.get("side") != t:
                continue
            row = {c: r.get(c) for c in _COLS}
            row["memory_id"] = row["memory_id"] or r.get("key")
            out.append({"op": _OPS.get(str(r.get("op")).upper(), str(r.get("op")).lower()), **row})
        return out

    def merge(self, name):
        self.vcs.merge(self._table(name), MAIN, "ACCEPT")

    def delete_branch(self, name):
        if name == "main":
            raise ValueError("不能删除 main 分支")
        self.vcs.drop_branch(self._table(name))
        if self.current_branch == name:
            self.current_branch = "main"
