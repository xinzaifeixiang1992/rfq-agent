"""MatrixOne 原生 Git4Data 实现。语法与输出格式已用 scripts/check_mo.py 在 MatrixOne v4.2.1 上验证。"""
from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from typing import Sequence

from app.db.conn import MatrixOneDatabase
from app.db.schema import VERSIONED_PK
from app.vcs.base import Conflict, DataVCS, DiffResult, DiffSummary, VCSConflict, ident

log = logging.getLogger(__name__)


@contextmanager
def _conflict_as_vcs():
    """MERGE/PICK ... WHEN CONFLICT FAIL 冲突时报 20101 "conflict: ..."，统一转为 VCSConflict。"""
    import pymysql
    try:
        yield
    except pymysql.err.MySQLError as e:
        if "conflict" in str(e).lower():
            raise VCSConflict(str(e.args[-1] if e.args else e)) from e
        raise


class MatrixOneVCS(DataVCS):
    backend = "matrixone"
    db: MatrixOneDatabase

    def snapshot(self, name, table):
        # 快照为账户级对象；FOR TABLE 子句中库名与表名以空格分隔
        self.db.execute(f"DROP SNAPSHOT IF EXISTS {ident(name)}")
        self.db.execute(f"CREATE SNAPSHOT {ident(name)} FOR TABLE {ident(self.db.database)} {ident(table)}")
        return name

    def read_at(self, table, snapshot, where="", params=()):
        cond = f" WHERE {where}" if where else ""
        return self.db.query(f"SELECT * FROM {ident(table)} {{snapshot = '{ident(snapshot)}'}}{cond}", params)

    def restore(self, table, snapshot):
        self.db.execute(f"RESTORE TABLE {ident(self.db.database)}.{ident(table)} {{SNAPSHOT = {ident(snapshot)}}}")

    @staticmethod
    def _base_table(branch: str) -> str:
        raw = f"_vcsbase_{branch}"
        return ident(raw if len(raw) <= 64 else f"_vcsbase_{hashlib.sha1(branch.encode()).hexdigest()[:20]}")

    def branch(self, name, source):
        name, source = ident(name), ident(source)
        base = self._base_table(name)
        self.db.execute(f"DROP TABLE IF EXISTS {name}")
        self.db.execute(f"DROP TABLE IF EXISTS {base}")
        # 原生 DIFF 若因云端 lineage 异常超时，以这份共同祖先副本做有界应用层降级。
        self.db.execute(f"CREATE TABLE {base} AS SELECT * FROM {source}")
        try:
            self.db.execute(f"DATA BRANCH CREATE TABLE {name} FROM {source}")
        except Exception:
            self.db.execute(f"DROP TABLE IF EXISTS {base}")
            raise
        return name

    @staticmethod
    def _changes(cur: dict, base: dict) -> dict:
        out = {}
        for key, row in cur.items():
            if key not in base:
                out[key] = ("INSERT", row)
            elif row != base[key]:
                out[key] = ("UPDATE", row)
        for key in base.keys() - cur.keys():
            out[key] = ("DELETE", None)
        return out

    @staticmethod
    def _summary(changes: dict) -> DiffSummary:
        ops = [op for op, _ in changes.values()]
        return DiffSummary(inserted=ops.count("INSERT"), updated=ops.count("UPDATE"), deleted=ops.count("DELETE"))

    def _fallback_diff(self, branch: str, against: str) -> DiffResult:
        pk = VERSIONED_PK.get(against)
        if not pk:
            raise ValueError(f"{against} 不是受版本管理的表")
        base_name = self._base_table(branch)
        base_table = base_name if self.db.table_exists(base_name) else against
        rows = lambda table: {r[pk]: r for r in self.db.query(f"SELECT * FROM {ident(table)}")}
        base = rows(base_table)
        branch_changes = self._changes(rows(branch), base)
        target_changes = self._changes(rows(against), base)
        detail = [{"side": branch, "op": op, "key": key, **(row or {})}
                  for key, (op, row) in branch_changes.items()]
        detail += [{"side": against, "op": op, "key": key, **(row or {})}
                   for key, (op, row) in target_changes.items()]
        return DiffResult(branch=self._summary(branch_changes), target=self._summary(target_changes), rows=detail)

    @staticmethod
    def _diff_unavailable(exc: Exception) -> bool:
        code = exc.args[0] if getattr(exc, "args", None) else None
        text = str(exc).lower()
        return code in (2006, 2013, 20101) or any(x in text for x in ("timed out", "timeout", "lineage"))

    def diff(self, branch, against):
        b, t = ident(branch), ident(against)
        if time.monotonic() < getattr(self, "_diff_backoff", 0.0):
            return self._fallback_diff(b, t)
        res = DiffResult()
        try:
            # SUMMARY 列：metric | <branch> | <against>，metric ∈ INSERTED/UPDATED/DELETED。
            # 独立短超时连接保证云端 lineage 异常不会把单次 RFQ 阻塞数分钟。
            for r in self.db.query_timeout(
                    f"DATA BRANCH DIFF {b} AGAINST {t} OUTPUT SUMMARY", self.db.diff_timeout):
                metric = str(r.get("metric", "")).lower()
                if metric in ("inserted", "updated", "deleted"):
                    setattr(res.branch, metric, int(r.get(b) or 0))
                    setattr(res.target, metric, int(r.get(t) or 0))
            # 行级列：'diff <b> against <t>'(取值为变更所在表) | flag | 表各列。
            pk = VERSIONED_PK.get(against)
            rows = self.db.query_timeout(
                f"DATA BRANCH DIFF {b} AGAINST {t} OUTPUT LIMIT 200", self.db.diff_timeout, utf8=True)
            for r in rows:
                items = list(r.items())
                data = dict(items[2:])
                key = data.get(pk) if pk else next(iter(data.values()), None)
                res.rows.append({"side": items[0][1], "op": r.get("flag"), "key": key, **data})
            return res
        except Exception as exc:
            if not self._diff_unavailable(exc):
                raise
            self._diff_backoff = time.monotonic() + 300
            log.warning("MatrixOne DATA BRANCH DIFF 不可用，5 分钟内降级为共同祖先应用层对比: %s", exc)
            return self._fallback_diff(b, t)

    def merge(self, branch, into, on_conflict: Conflict = "FAIL"):
        with _conflict_as_vcs():
            self.db.execute(f"DATA BRANCH MERGE {ident(branch)} INTO {ident(into)} WHEN CONFLICT {on_conflict}")

    def pick(self, branch, into, keys: Sequence[str], on_conflict: Conflict = "ACCEPT"):
        if not keys:
            return
        ph = ", ".join(["%s"] * len(keys))
        with _conflict_as_vcs():
            self.db.execute(
                f"DATA BRANCH PICK {ident(branch)} INTO {ident(into)} KEYS ({ph}) WHEN CONFLICT {on_conflict}",
                list(keys))

    def drop_branch(self, name):
        name = ident(name)
        self.db.execute(f"DROP TABLE IF EXISTS {name}")
        self.db.execute(f"DROP TABLE IF EXISTS {self._base_table(name)}")

    def drop_snapshots(self) -> int:
        """删除本库的表级快照（演示重置用）。"""
        n = 0
        for r in self.db.query("SHOW SNAPSHOTS"):
            row = {k.lower(): v for k, v in r.items()}
            if row.get("database_name") == self.db.database:
                self.db.execute(f"DROP SNAPSHOT IF EXISTS {ident(row['snapshot_name'])}")
                n += 1
        return n
