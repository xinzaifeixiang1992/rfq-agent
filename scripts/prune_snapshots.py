"""回收 MatrixOne 表级快照。

MatrixOne 的表级快照是账户级对象，上限 50 个；本应用每进一张询价单会用掉 2 个
（cost_price / pricing_rule）。超过上限后 CREATE SNAPSHOT 报
`20101 ... reached the limit of 50`，新单在 intake 阶段直接 503。

用法：
    python scripts/prune_snapshots.py --dry-run      # 只看会删哪些
    python scripts/prune_snapshots.py                # 保留最近 30 个，删更早的
    python scripts/prune_snapshots.py --keep 10      # 更激进地回收
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="回收 MatrixOne 表级快照")
    ap.add_argument("--keep", type=int, default=30, help="保留最近 N 个（默认 30，上限 50）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    a = ap.parse_args()

    s = get_settings()
    if not (s.mo_host and s.mo_user):
        print("当前不是 MatrixOne 后端（.env 未配 MO_HOST/MO_USER）")
        return 1

    from app.db.conn import MatrixOneDatabase
    from app.vcs.matrixone import MatrixOneVCS

    db = MatrixOneDatabase(s)
    vcs = MatrixOneVCS(db)
    all_snaps = vcs._snapshots()
    snaps = [x for x in all_snaps if x[1].lower().startswith(MatrixOneVCS.SNAPSHOT_PREFIX)]
    print(f"库 {db.database} · 表级快照共 {len(all_snaps)} 个（上限 50），其中询价单快照 {len(snaps)} 个")
    print("只回收询价单快照，baseline / pre_merge_* 等记忆快照不动")
    if snaps:
        print(f"  最早：{snaps[0][1]}")
        print(f"  最新：{snaps[-1][1]}")

    victims = snaps[: max(0, len(snaps) - a.keep)]
    if not victims:
        print(f"未超过保留数 {a.keep}，无需回收")
        return 0

    for _, name in victims:
        print(f"  {'[dry] ' if a.dry_run else 'drop '}{name}")
    if a.dry_run:
        print(f"\n将删除 {len(victims)} 个，保留 {a.keep} 个；去掉 --dry-run 执行")
        return 0

    n = vcs.prune_snapshots(a.keep)
    print(f"\n已删除 {n} 个，剩余 {len(snaps) - n} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
