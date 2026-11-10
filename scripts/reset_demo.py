"""清零演示数据：清空业务库（含表级快照）→ 重新灌入合成数据 → 清空上传/输出目录。

不会动的东西：.env、samples/、以及 Memoria 云端记忆（它不随业务库重置）。

用法：
    python scripts/reset_demo.py --dry-run
    python scripts/reset_demo.py --yes
    python scripts/reset_demo.py --yes --strong qwen3.8-max --keep-files
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.memory import create_memory  # noqa: E402
from app.seed import drop_all, seed, seed_memories  # noqa: E402
from app.services import build_services  # noqa: E402


def _count(db, table: str) -> int | str:
    try:
        return db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
    except Exception:
        return "-"


def _purge(dir_: Path, dry: bool) -> int:
    """删除目录下 RFQ 开头的子目录，返回命中数量。"""
    if not dir_.exists():
        return 0
    n = 0
    for p in dir_.iterdir():
        if p.is_dir() and p.name.upper().startswith("RFQ"):
            n += 1
            if not dry:
                shutil.rmtree(p, ignore_errors=True)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="清零演示数据并重建演示库")
    ap.add_argument("--yes", action="store_true", help="确认执行（否则只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要改动的内容")
    ap.add_argument("--keep-files", action="store_true", help="保留 data/uploads 与 outputs 目录")
    ap.add_argument("--fast", help="覆盖 MODEL_FAST")
    ap.add_argument("--strong", help="覆盖 MODEL_STRONG")
    ap.add_argument("--vision", help="覆盖 MODEL_VISION")
    a = ap.parse_args()

    s = get_settings()
    svc = build_services(s)
    models = {"fast": a.fast or s.model_fast, "strong": a.strong or s.model_strong,
              "vision": a.vision or s.model_vision}

    print(f"后端：db={svc.db.dialect} vcs={svc.vcs.backend} memory={svc.memory.backend}")
    print(f"将写入模型：fast={models['fast']} strong={models['strong']} vision={models['vision']}")
    print(f"当前数据：询价单 {_count(svc.db, 'rfq_record')} 张 · "
          f"报价行 {_count(svc.db, 'quote_line')} 条 · 审计 {_count(svc.db, 'audit_event')} 条")
    print(f"目录：uploads={_purge(ROOT / 'data' / 'uploads', True)} 组 · "
          f"outputs={_purge(ROOT / 'outputs', True)} 组")

    if a.dry_run or not a.yes:
        print("\n[dry] 将 DROP 全部业务表与表级快照，重新 seed 合成数据；"
              f"{'保留' if a.keep_files else '清空'} uploads/outputs")
        print("确认执行请加 --yes")
        return 0

    drop_all(svc.db)
    svc.memory = create_memory(s, svc.db)
    stats = seed(svc.db, models=models)
    with svc.memory.on_branch("main"):
        empty = not svc.memory.list(1)
    stats["memories"] = seed_memories(svc.memory) if empty else 0
    print(f"\n已重置：{stats}")

    if not a.keep_files:
        print(f"已清理目录：uploads={_purge(ROOT / 'data' / 'uploads', False)} 组 · "
              f"outputs={_purge(ROOT / 'outputs', False)} 组")
    if svc.memory.backend == "memoria":
        print("提示：Memoria 云端记忆独立于业务库，本次未清空。需要清记忆请另行说明。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
