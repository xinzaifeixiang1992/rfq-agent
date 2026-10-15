"""数据库连接。SQL 统一使用 %s 占位符，SQLite 下自动转换为 ?。"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable, Sequence

from app.config import Settings


class Database:
    dialect: str = ""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        raise NotImplementedError

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        raise NotImplementedError

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        raise NotImplementedError

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def table_exists(self, table: str) -> bool:
        raise NotImplementedError

    def columns(self, table: str) -> list[str]:
        raise NotImplementedError


class SQLiteDatabase(Database):
    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        super().__init__()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("%s", "?")

    def query(self, sql, params=()):
        with self._lock:
            cur = self.conn.execute(self._sql(sql), tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def execute(self, sql, params=()):
        with self._lock:
            return self.conn.execute(self._sql(sql), tuple(params)).rowcount

    def executemany(self, sql, rows):
        with self._lock:  # 自动提交模式下逐行落盘很慢：批量写入放进一个事务
            in_tx = self.conn.in_transaction
            if not in_tx:
                self.conn.execute("BEGIN")
            try:
                self.conn.executemany(self._sql(sql), [tuple(r) for r in rows])
            except Exception:
                if not in_tx:
                    self.conn.execute("ROLLBACK")
                raise
            if not in_tx:
                self.conn.execute("COMMIT")

    def table_exists(self, table):
        return bool(self.query("SELECT name FROM sqlite_master WHERE type='table' AND name=%s", (table,)))

    def columns(self, table):
        return [r["name"] for r in self.query(f"PRAGMA table_info({table})")]


class MatrixOneDatabase(Database):
    dialect = "matrixone"

    def __init__(self, s: Settings) -> None:
        super().__init__()
        import pymysql

        self.database = s.mo_database
        self.diff_timeout = s.mo_diff_timeout
        kw = dict(host=s.mo_host, port=s.mo_port, user=s.mo_user, password=s.mo_password,
                  connect_timeout=s.mo_connect_timeout, read_timeout=s.mo_read_timeout,
                  write_timeout=s.mo_write_timeout, charset="utf8mb4", autocommit=True,
                  cursorclass=pymysql.cursors.DictCursor)
        if s.mo_init_command:  # 每次（重）连接后执行；含多条语句时需开启 MULTI_STATEMENTS
            kw["init_command"] = s.mo_init_command
            if ";" in s.mo_init_command.strip().rstrip(";"):
                from pymysql.constants import CLIENT
                kw["client_flag"] = CLIENT.MULTI_STATEMENTS
        boot = pymysql.connect(**kw)
        with boot.cursor() as c:
            c.execute(f"CREATE DATABASE IF NOT EXISTS {self.database}")
        boot.close()
        self._kw = dict(database=self.database, **kw)
        self.conn = pymysql.connect(**self._kw)

    def _cursor(self):
        # Serverless 实例会回收空闲连接：探活失败则重建（pymysql 已弃用 ping(reconnect=True)）
        import pymysql
        try:
            self.conn.ping()
        except pymysql.err.Error:
            self.conn = pymysql.connect(**self._kw)
        return self.conn.cursor()

    def query(self, sql, params=()):
        with self._lock, self._cursor() as c:
            c.execute(sql, tuple(params) or None)
            return list(c.fetchall() or [])

    def query_timeout(self, sql, timeout: int, *, utf8: bool = False, params=()):
        """用独立连接执行有界查询；超时连接直接关闭，避免污染共享连接。用于可能长期挂起的 Git4Data DIFF。"""
        import pymysql

        kw = {**self._kw, "read_timeout": timeout}
        conn = pymysql.connect(**kw)
        conn.use_unicode = not utf8
        try:
            with conn.cursor() as c:
                c.execute(sql, tuple(params) or None)
                rows = list(c.fetchall() or [])
        finally:
            conn.close()
        if not utf8:
            return rows
        return [{k: v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else v for k, v in r.items()}
                for r in rows]

    def query_utf8(self, sql, params=()):
        """按字节取回再以 UTF-8 解码。DATA BRANCH DIFF 行输出把 TEXT 列标为类型 241，pymysql 会误用 ascii 解码。"""
        with self._lock, self._cursor() as c:
            self.conn.use_unicode = False
            try:
                c.execute(sql, tuple(params) or None)
                rows = list(c.fetchall() or [])
            finally:
                self.conn.use_unicode = True
        return [{k: v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else v for k, v in r.items()}
                for r in rows]

    def execute(self, sql, params=()):
        with self._lock, self._cursor() as c:
            return c.execute(sql, tuple(params) or None)

    def executemany(self, sql, rows):
        with self._lock, self._cursor() as c:
            c.executemany(sql, [tuple(r) for r in rows])

    def table_exists(self, table):
        return bool(self.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
            (self.database, table)))

    def columns(self, table):
        rows = self.query(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (self.database, table))
        return [r["name"] for r in rows]


def create_database(s: Settings) -> Database:
    if s.db_backend == "matrixone":
        return MatrixOneDatabase(s)
    return SQLiteDatabase(str(s.path(s.sqlite_path)))
