"""初始化/重置演示数据库。用法：python scripts/seed.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import drop_all, seed, seed_memories  # noqa: E402
from app.services import build_services  # noqa: E402


def main() -> None:
    s = build_services()
    drop_all(s.db)
    s = build_services()
    st = s.settings
    stats = seed(s.db, models={"fast": st.model_fast, "strong": st.model_strong, "vision": st.model_vision})
    stats["memories"] = seed_memories(s.memory)
    print(f"[{s.db.dialect} / {s.vcs.backend} / memory={s.memory.backend}]", stats)


if __name__ == "__main__":
    main()
