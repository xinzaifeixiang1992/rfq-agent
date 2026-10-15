"""MatrixOne 连通性与 Git4Data 能力探测。只读写 _probe_ 前缀的临时表，结束后清理。

用法：在 .env 中填好 MO_HOST / MO_PORT / MO_USER / MO_PASSWORD / MO_DATABASE 后运行
    python scripts/check_mo.py
逐条输出每个 Git4Data 语句是否可用、报错原文与返回列格式，用于校准 app/vcs/matrixone.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

T, B, S = "_probe_main", "_probe_br", "_probe_snap"


def main() -> None:
    s = get_settings()
    if not (s.mo_host and s.mo_user):
        sys.exit("请先在 .env 中填写 MO_HOST / MO_USER / MO_PASSWORD")
    from app.db.conn import MatrixOneDatabase

    db = MatrixOneDatabase(s)
    print("version:", db.query("SELECT version() AS v")[0]["v"], "| database:", db.database)

    def step(title: str, sql: str, show: bool = False) -> list | None:
        try:
            rows = db.query(sql)
            print(f"[OK]   {title}")
            if show:
                for r in rows[:6]:
                    print("         ", r)
            return rows
        except Exception as e:  # noqa: BLE001 —— 探测脚本需要原样输出各种驱动错误
            print(f"[FAIL] {title}\n         {sql}\n         {type(e).__name__}: {e}")
            return None

    for sql in (f"DROP TABLE IF EXISTS {B}", f"DROP TABLE IF EXISTS {T}", f"DROP SNAPSHOT IF EXISTS {S}"):
        step("cleanup", sql)
    step("create table", f"CREATE TABLE {T} (k VARCHAR(16) PRIMARY KEY, v VARCHAR(16))")
    step("insert", f"INSERT INTO {T} VALUES ('a','1'),('b','2'),('c','3')")

    step("snapshot", f"CREATE SNAPSHOT {S} FOR TABLE {db.database} {T}")
    step("update main", f"UPDATE {T} SET v='9' WHERE k='a'")
    step("read at snapshot", f"SELECT * FROM {T} {{snapshot = '{S}'}}", show=True)
    step("restore", f"RESTORE TABLE {db.database}.{T} {{SNAPSHOT = {S}}}")
    step("after restore", f"SELECT * FROM {T} ORDER BY k", show=True)

    step("branch create", f"DATA BRANCH CREATE TABLE {B} FROM {T}")
    step("branch update", f"UPDATE {B} SET v='x' WHERE k IN ('a','b')")
    step("branch insert", f"INSERT INTO {B} VALUES ('d','4')")
    step("main update (conflict on a)", f"UPDATE {T} SET v='m' WHERE k='a'")
    step("diff summary", f"DATA BRANCH DIFF {B} AGAINST {T} OUTPUT SUMMARY", show=True)
    step("diff rows", f"DATA BRANCH DIFF {B} AGAINST {T} OUTPUT LIMIT 20", show=True)
    step("pick b", f"DATA BRANCH PICK {B} INTO {T} KEYS ('b') WHEN CONFLICT ACCEPT")
    step("merge FAIL (expect conflict)", f"DATA BRANCH MERGE {B} INTO {T} WHEN CONFLICT FAIL")
    step("merge SKIP", f"DATA BRANCH MERGE {B} INTO {T} WHEN CONFLICT SKIP")
    step("after merge", f"SELECT * FROM {T} ORDER BY k", show=True)
    step("drop branch (DROP TABLE)", f"DROP TABLE IF EXISTS {B}")

    for sql in (f"DROP TABLE IF EXISTS {T}", f"DROP SNAPSHOT IF EXISTS {S}"):
        step("cleanup", sql)


if __name__ == "__main__":
    main()
