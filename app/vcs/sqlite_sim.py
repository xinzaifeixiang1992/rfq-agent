"""Git4Data 语义的 SQLite 仿真，仅用于本地开发与单元测试。

语义对齐 MatrixOne：分支记录血缘（共同祖先），DIFF 计算双方相对祖先的净变更，
只有"两边都改了同一行"才算冲突；MERGE/PICK 支持 FAIL / SKIP / ACCEPT。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Sequence

from app.db.schema import VERSIONED_PK
from app.vcs.base import Conflict, DataVCS, DiffResult, DiffSummary, VCSConflict, ident


class SQLiteSimVCS(DataVCS):
    backend = "sqlite-sim"

    def __init__(self, db) -> None:
        super().__init__(db)
        db.execute("CREATE TABLE IF NOT EXISTS _vcs_branch (name TEXT PRIMARY KEY, source TEXT, created_at TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS _vcs_snapshot (name TEXT, table_name TEXT, copy_table TEXT, "
                   "created_at TEXT, PRIMARY KEY (name, table_name))")

    # ---------- 内部工具 ----------
    def _clone_table(self, src: str, dst: str) -> None:
        ddl = self.db.query_one("SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", (src,))
        if not ddl:
            raise ValueError(f"表不存在: {src}")
        new_ddl = re.sub(rf"CREATE TABLE (IF NOT EXISTS )?{src}\b", f"CREATE TABLE {dst}", ddl["sql"], count=1)
        self.db.execute(f"DROP TABLE IF EXISTS {dst}")
        self.db.execute(new_ddl)
        self.db.execute(f"INSERT INTO {dst} SELECT * FROM {src}")

    def _root(self, table: str) -> str:
        """沿血缘追溯到受版本管理的根表，以确定主键。"""
        seen = set()
        while table not in VERSIONED_PK and table not in seen:
            seen.add(table)
            row = self.db.query_one("SELECT source FROM _vcs_branch WHERE name=%s", (table,))
            if not row:
                break
            table = row["source"]
        return table

    def _pk(self, table: str) -> str:
        root = self._root(table)
        if root not in VERSIONED_PK:
            raise ValueError(f"{table} 不是受版本管理的表")
        return VERSIONED_PK[root]

    def _rows(self, table: str, pk: str) -> dict:
        return {r[pk]: r for r in self.db.query(f"SELECT * FROM {table}")}

    @staticmethod
    def _changes(cur: dict, base: dict) -> dict:
        """相对祖先的净变更：key -> (op, row|None)。"""
        out = {}
        for k, r in cur.items():
            if k not in base:
                out[k] = ("INSERT", r)
            elif r != base[k]:
                out[k] = ("UPDATE", r)
        for k in base.keys() - cur.keys():
            out[k] = ("DELETE", None)
        return out

    @staticmethod
    def _summary(ch: dict) -> DiffSummary:
        ops = [op for op, _ in ch.values()]
        return DiffSummary(inserted=ops.count("INSERT"), updated=ops.count("UPDATE"), deleted=ops.count("DELETE"))

    def _apply(self, branch: str, into: str, keys: set | None, on_conflict: Conflict) -> None:
        pk = self._pk(branch)
        base = self._rows(f"_base_{branch}", pk)
        b_ch = self._changes(self._rows(branch, pk), base)
        t_ch = self._changes(self._rows(into, pk), base)
        if keys is not None:
            b_ch = {k: v for k, v in b_ch.items() if k in keys}
        conflicts = {k for k, v in b_ch.items() if k in t_ch and t_ch[k] != v}
        if conflicts and on_conflict == "FAIL":
            raise VCSConflict(f"{len(conflicts)} 行冲突: {sorted(conflicts)[:5]}")
        with self.db._lock:
            for k, (op, row) in b_ch.items():
                if k in conflicts and on_conflict == "SKIP":
                    continue
                self.db.execute(f"DELETE FROM {into} WHERE {pk}=%s", (k,))
                if op != "DELETE":
                    cols = list(row.keys())
                    self.db.execute(f"INSERT INTO {into} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                                    [row[c] for c in cols])

    # ---------- 公共接口 ----------
    def snapshot(self, name, table):
        copy = f"_snap_{ident(name)}_{ident(table)}"
        self._clone_table(table, copy)
        self.db.execute("INSERT OR REPLACE INTO _vcs_snapshot VALUES (%s, %s, %s, %s)",
                        (name, table, copy, datetime.now().isoformat(timespec="seconds")))
        return name

    def _snap_table(self, table, snapshot) -> str:
        row = self.db.query_one("SELECT copy_table FROM _vcs_snapshot WHERE name=%s AND table_name=%s",
                                (snapshot, table))
        if not row:
            raise ValueError(f"快照不存在: {snapshot}/{table}")
        return row["copy_table"]

    def read_at(self, table, snapshot, where="", params=()):
        cond = f" WHERE {where}" if where else ""
        return self.db.query(f"SELECT * FROM {self._snap_table(table, snapshot)}{cond}", params)

    def restore(self, table, snapshot):
        copy = self._snap_table(table, snapshot)
        with self.db._lock:
            self.db.execute(f"DELETE FROM {ident(table)}")
            self.db.execute(f"INSERT INTO {table} SELECT * FROM {copy}")

    def branch(self, name, source):
        ident(name), ident(source)
        self._clone_table(source, name)
        self._clone_table(source, f"_base_{name}")
        self.db.execute("INSERT OR REPLACE INTO _vcs_branch VALUES (%s, %s, %s)",
                        (name, source, datetime.now().isoformat(timespec="seconds")))
        return name

    def diff(self, branch, against):
        pk = self._pk(branch)
        base = self._rows(f"_base_{ident(branch)}", pk)
        b_ch = self._changes(self._rows(branch, pk), base)
        t_ch = self._changes(self._rows(ident(against), pk), base)
        rows = [{"side": branch, "op": op, "key": k, **(r or {})} for k, (op, r) in b_ch.items()]
        rows += [{"side": against, "op": op, "key": k, **(r or {})} for k, (op, r) in t_ch.items()]
        return DiffResult(branch=self._summary(b_ch), target=self._summary(t_ch), rows=rows)

    def merge(self, branch, into, on_conflict: Conflict = "FAIL"):
        self._apply(ident(branch), ident(into), None, on_conflict)

    def pick(self, branch, into, keys: Sequence[str], on_conflict: Conflict = "ACCEPT"):
        self._apply(ident(branch), ident(into), set(keys), on_conflict)

    def drop_branch(self, name):
        self.db.execute(f"DROP TABLE IF EXISTS {ident(name)}")
        self.db.execute(f"DROP TABLE IF EXISTS _base_{name}")
        self.db.execute("DELETE FROM _vcs_branch WHERE name=%s", (name,))
