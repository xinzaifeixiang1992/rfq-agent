"""校准模型路由：把 model_route 里的模型名换成 LLM 网关实际可用的名字。

路由配置存在数据库（配置即数据），改 Railway 的 MODEL_* 变量不会影响已有库，
只有在建库或 /admin/reset 时才会重新灌入。

用法：
    python scripts/fix_model_route.py --list
    python scripts/fix_model_route.py --strong qwen3.8-max
    python scripts/fix_model_route.py --fast deepseek-v4.1-flash --strong qwen3.8-max --vision qwen3.8-max
    python scripts/fix_model_route.py --strong glm-5.3 --cost 0.0024   # 顺带补 model_catalog 成本行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

TEXT_TASKS = ("intake_extract", "body_items", "reply_email")
VISION_TASKS = ("vision_items",)


def show(db) -> None:
    print("model_catalog:")
    for r in db.query("SELECT model, kind, cost_per_1k FROM model_catalog"):
        print(f"  {r}")
    print("model_route:")
    for r in db.query("SELECT task, primary_model, escalate_model, fallback_model, confidence_threshold "
                      "FROM model_route"):
        print(f"  {r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="校准 MatrixOne 中的模型路由配置")
    ap.add_argument("--list", action="store_true", help="只打印当前路由与成本表")
    ap.add_argument("--fast", help="文本任务的主模型（intake_extract/body_items/reply_email）")
    ap.add_argument("--strong", help="文本任务的升级模型")
    ap.add_argument("--vision", help="视觉任务模型（vision_items）")
    ap.add_argument("--cost", type=float, help="为本次写入的模型补 model_catalog 成本（美元/1k token）")
    ap.add_argument("--kind", default="llm", choices=["llm", "vlm", "rule"], help="补 catalog 时的类型")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的改动")
    a = ap.parse_args()

    s = get_settings()
    if not (s.mo_host and s.mo_user):
        print("当前不是 MatrixOne 后端（.env 未配 MO_HOST/MO_USER），本脚本只处理线上库")
        return 1

    from app.db.conn import MatrixOneDatabase

    db = MatrixOneDatabase(s)
    print(f"database: {db.database} @ {s.mo_host}:{s.mo_port}\n")

    if a.list or not any((a.fast, a.strong, a.vision)):
        show(db)
        if not a.list:
            print("\n未指定要改的档位；加 --fast / --strong / --vision 才会写入")
        return 0

    changes: list[tuple[str, str, str]] = []  # (task, 列, 模型)
    if a.fast:
        changes += [(t, "primary_model", a.fast) for t in TEXT_TASKS]
    if a.strong:
        changes += [(t, "escalate_model", a.strong) for t in TEXT_TASKS]
    if a.vision:
        changes += [(t, "primary_model", a.vision) for t in VISION_TASKS]
        changes += [(t, "escalate_model", a.vision) for t in VISION_TASKS]

    for task, col, model in changes:
        print(f"{'[dry] ' if a.dry_run else ''}UPDATE model_route SET {col}={model} WHERE task={task}")
    if a.dry_run:
        return 0

    for task, col, model in changes:
        db.execute(f"UPDATE model_route SET {col}=%s WHERE task=%s", (model, task))

    for model in {m for _, _, m in changes}:
        if a.cost is not None:
            db.execute("DELETE FROM model_catalog WHERE model=%s", (model,))
            db.execute("INSERT INTO model_catalog VALUES (%s,%s,%s)", (model, a.kind, a.cost))
            print(f"catalog: {model} -> {a.kind}, cost_per_1k={a.cost}")
        elif not db.query_one("SELECT model FROM model_catalog WHERE model=%s", (model,)):
            print(f"警告：model_catalog 缺少 {model}，成本会记 0；可加 --cost 补一行")

    print()
    show(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
