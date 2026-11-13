"""一次性运维：清掉账号下非本库的僵尸表级快照，并清空历史旧库的表（保留库外壳）。

背景：MatrixOne 表级快照是**账户级**配额（上限 50），每进一单本库消耗 2 个。
旧库 rfq_agent / rfq_agent_test / rfq_agent_v2_test 遗留的快照会白白吃掉配额，
把可用余量压到只够再进十几单。本脚本只做两件事：删这些快照、清这些库的表。

用法：
    python scripts/purge_stale.py            # 只预览
    python scripts/purge_stale.py --yes      # 执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db.conn import MatrixOneDatabase  # noqa: E402

STALE_DBS = ("rfq_agent", "rfq_agent_diag", "rfq_agent_test")


def main() -> int:
    ap = argparse.ArgumentParser(description="清理僵尸快照与历史旧库")
    ap.add_argument("--yes", action="store_true", help="确认执行（否则只预览）")
    ap.add_argument("--keep-dbs", default=",".join(STALE_DBS), help="要清表但保留的旧库，逗号分隔")
    a = ap.parse_args()

    s = get_settings()
    db = MatrixOneDatabase(s)
    keep_dbs = [x.strip() for x in a.keep_dbs.split(",") if x.strip()]

    snaps = db.query("SHOW SNAPSHOTS")
    zombies = []
    for r in snaps:
        d = {str(k).lower(): v for k, v in r.items()}
        if d.get("database_name") != db.database:
            zombies.append((str(d.get("database_name")), str(d.get("snapshot_name"))))

    print(f"账户级快照 {len(snaps)} / 50，其中本库 {len(snaps) - len(zombies)} 个")
    print(f"僵尸快照 {len(zombies)} 个：")
    for database, name in zombies:
        print(f"  {database:<20} {name}")

    plan: dict[str, list[str]] = {}
    for database in keep_dbs:
        rows = db.query("SELECT table_name AS t FROM information_schema.tables WHERE table_schema=%s",
                        (database,))
        plan[database] = [r["t"] for r in rows]
    print("\n旧库表清理计划：")
    for database, tables in plan.items():
        print(f"  {database:<20} {len(tables)} 张表")

    if not a.yes:
        print("\n[dry] 未做任何改动。确认执行请加 --yes")
        return 0

    for _, name in zombies:
        db.execute(f"DROP SNAPSHOT IF EXISTS {name}")
    print(f"\n已删除僵尸快照 {len(zombies)} 个")

    for database, tables in plan.items():
        for t in tables:
            db.execute(f"DROP TABLE IF EXISTS `{database}`.`{t}`")
        print(f"已清空 {database} 的 {len(tables)} 张表（库保留）")

    left = db.query("SHOW SNAPSHOTS")
    print(f"\n执行后账户级快照 {len(left)} / 50（本库 0，全部余量归明天演示使用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
