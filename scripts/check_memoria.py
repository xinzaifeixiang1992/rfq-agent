"""Memoria 云服务连通性与接口探测。只使用 rfq_probe_ 前缀的临时快照/分支与一条探测记忆，结束后清理。

用法：在 .env 中填写 MEMORIA_URL=https://api.thememoria.ai 与 MEMORIA_API_KEY（thememoria.ai/app 申请）后运行
    python scripts/check_memoria.py
逐步输出每个接口的状态码与原始返回，用于校准 app/memory/memoria.py 的字段解析。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.memory.memoria import MemoriaStore  # noqa: E402

SNAP, BR = "rfq_probe_snap", "rfq_probe_br"


def show(payload, n: int = 600) -> str:
    s = json.dumps(payload, ensure_ascii=False, default=str) if not isinstance(payload, str) else payload
    return s if len(s) <= n else s[:n] + " ..."


def main() -> None:
    s = get_settings()
    url = (s.memoria_url or "https://api.thememoria.ai").rstrip("/")
    h = httpx.get(f"{url}/health", timeout=15)
    print(f"health {h.status_code}: {h.text[:200]}")
    if not s.memoria_api_key:
        sys.exit("未配置 MEMORIA_API_KEY：请在 thememoria.ai/app 申请 API Key 并写入 .env（MOI 的 PAT 不通用）")

    m = MemoriaStore(url, s.memoria_api_key)
    raw = m._call

    def logged(method, path, **kw):
        out = raw(method, path, **kw)
        print(f"         {method} {path} -> {show(out)}")
        return out

    m._call = logged
    ok = True

    def step(title, fn, required=True):
        nonlocal ok
        try:
            r = fn()
            print(f"[OK]   {title}" + (f"  => {show(r, 300)}" if r not in (None, "") else ""))
            return r
        except Exception as e:  # noqa: BLE001 —— 探测脚本需要原样输出各种错误
            ok = ok and not required
            print(f"[{'FAIL' if required else 'WARN'}] {title}\n         {type(e).__name__}: {e}")
            return None

    m.retries = 0  # 探测时 429 直接暴露
    step("clean stale probe branch", lambda: m.delete_branch(BR), required=False)
    step("clean stale probe snapshot", lambda: m.delete_snapshot(SNAP), required=False)
    step("list", lambda: len(m.list(5)))
    mid = step("store", lambda: m.store("rfq-agent 探测记忆：Probe Co. 偏好 DAP 交付", kind="profile",
                                        customer_id="PROBE", trust_tier="T4", source="check_memoria"))
    step("retrieve", lambda: [(i.memory_id, i.content, i.score) for i in m.retrieve("Probe DAP 交付", "PROBE", 3)])
    step("snapshot", lambda: m.snapshot(SNAP, "rfq-agent probe"))
    step("snapshots", lambda: len(m.snapshots()))
    step("branch", lambda: m.branch(BR))
    step("checkout branch", lambda: m.checkout(BR))
    step("store on branch", lambda: m.store("rfq-agent 探测：分支内新增", kind="semantic", source="check_memoria"))
    step("list on branch", lambda: [i.content for i in m.list(5)])
    step("checkout main", lambda: m.checkout("main"))
    step("branch diff", lambda: m.diff(BR))
    step("merge", lambda: m.merge(BR))
    step("delete branch", lambda: m.delete_branch(BR))
    step("snapshot diff", lambda: m.snapshot_diff(SNAP))
    step("rollback to snapshot", lambda: m.rollback(SNAP))  # 撤销合并进来的分支记忆
    if mid:
        step("feedback", lambda: m.feedback(mid, "useful", "check_memoria"), required=False)
        step(f"forget {mid}", lambda: m.forget(mid, reason="check_memoria cleanup"))
    step("governance（有 1 小时冷却，失败不计）", lambda: m.governance(False), required=False)
    step("delete snapshot", lambda: m.delete_snapshot(SNAP))
    print("ALL OK" if ok else "存在失败步骤：按上方原始返回调整 app/memory/memoria.py")


if __name__ == "__main__":
    main()
