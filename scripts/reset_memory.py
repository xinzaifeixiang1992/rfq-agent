"""把记忆空间恢复到初始态：删非主干分支、删快照、清空 main 上的记忆，再灌入初始记忆与 baseline 快照。

默认只清理本应用创建的对象（m_rfq* / pre_merge_* / pre_quarantine_* 等前缀与本地库快照），
不触碰你在 Memoria 控制台手工建的快照；需要全清时加 --all。

用法：
    python scripts/reset_memory.py --dry-run
    python scripts/reset_memory.py --yes
    python scripts/reset_memory.py --yes --all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.memory.base import MemoryStore  # noqa: E402
from app.seed import seed_memories  # noqa: E402
from app.services import build_services  # noqa: E402

LOCAL_SNAPSHOT_TABLES = ("memory_snapshot", "mem_snapshot_meta")


def _owned(name: str) -> bool:
    prefixes = ("m_rfq", "m_r_smoke_", "pre_merge_", "pre_quarantine_", "baseline")
    return name.startswith(prefixes)


def _clear_local(db) -> int:
    """local / git4data 后端：快照与记忆都在业务库里，直接按表清空。"""
    n = 0
    for t in LOCAL_SNAPSHOT_TABLES:
        try:
            db.execute(f"DELETE FROM {t}")
            n += 1
        except Exception:
            continue
    return n


def _clear_memoria(mem, purge_all: bool, dry: bool) -> tuple[list[str], list[str]]:
    branches = [b for b in getattr(mem, "branches", lambda: [])() if b != "main"]
    snaps = [str(s.get("name")) for s in mem.snapshots()
             if purge_all or _owned(str(s.get("name")))]
    if dry:
        return branches, snaps
    for b in branches:
        mem.delete_branch(b)
    for name in snaps:
        mem.delete_snapshot(name)
    with mem.on_branch("main"):
        for it in mem.list(500):
            mem.forget(it.memory_id, reason="demo reset")
    return branches, snaps


def main() -> int:
    ap = argparse.ArgumentParser(description="把记忆空间恢复到初始态")
    ap.add_argument("--yes", action="store_true", help="确认执行")
    ap.add_argument("--dry-run", action="store_true", help="只打印将清理的对象")
    ap.add_argument("--all", action="store_true", help="连非本应用前缀的快照一起删")
    a = ap.parse_args()

    s = get_settings()
    svc = build_services(s)
    mem: MemoryStore = svc.memory

    with mem.on_branch("main"):
        items = mem.list(500)
    print(f"后端：{mem.backend}")
    print(f"main 分支记忆 {len(items)} 条")
    if mem.backend == "memoria":
        print(f"分支：{getattr(mem, 'branches', lambda: [])()}")
        print(f"快照：{[str(x.get('name')) for x in mem.snapshots()]}")

    if a.dry_run or not a.yes:
        print("\n[dry] 将删除非 main 分支、"
              f"{'全部' if a.all else '本应用创建的'}快照、main 上全部记忆，然后重新灌入初始记忆")
        print("确认执行请加 --yes")
        return 0

    if mem.backend == "memoria":
        branches, snaps = _clear_memoria(mem, a.all, False)
        print(f"已删分支 {len(branches)} 个：{branches}")
        print(f"已删快照 {len(snaps)} 个：{snaps}")
    else:
        n = _clear_local(svc.db)
        print(f"已清空本地记忆快照表 {n} 个")
        with mem.on_branch("main"):
            for it in mem.list(500):
                mem.forget(it.memory_id, reason="demo reset")

    with mem.on_branch("main"):
        n = seed_memories(mem)
    print(f"已重新灌入初始记忆 {n} 条，并创建 baseline 快照")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
