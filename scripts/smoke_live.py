"""对运行中的 API 做端到端冒烟（真实 LLM + 当前数据后端）。用法：python scripts/smoke_live.py

覆盖：4 封样本进件 → 整单 MERGE（含人工改价）/ 部分 PICK / 驳回 → 重放 → 规则沙盒 → 记忆快照回滚
      → MemoryGate（投毒邮件拦截、隔离分支评审合并、历史遗留投毒扫描隔离 + 影响分析）。
会写入数据，建议先 POST /admin/reset 后运行；历史投毒步骤需 ALLOW_ADMIN_RESET=true 且配置 ADMIN_TOKEN。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import get_settings  # noqa: E402

API = get_settings().api_base
c = httpx.Client(base_url=API, timeout=300, headers={"X-Admin-Token": get_settings().admin_token})


def ok(r: httpx.Response) -> dict | list:
    if r.status_code >= 400:
        raise SystemExit(f"[FAIL] {r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def step(title: str, fn):
    t = time.perf_counter()
    out = fn()
    print(f"[OK] {title:<34} {time.perf_counter() - t:6.1f}s")
    return out


def main() -> None:
    print("health:", ok(c.get("/health")))
    rfqs = {}
    for f in sorted((ROOT / "samples").glob("*.eml")):
        v = step(f"intake {f.name}", lambda f=f: ok(c.post("/rfq", files={"eml": (f.name, f.read_bytes())})))
        st = v["state"]
        rfqs[f.stem] = v
        hits = [m.get("match_method") for m in st.get("matched", [])]
        print(f"     {v['rfq_id']} status={(v.get('record') or {}).get('status')} lines={len(hits)} "
              f"match={ {k: hits.count(k) for k in sorted(set(hits))} } rec={st.get('recommended')} "
              f"clarify={'yes' if st.get('clarification') else 'no'}")

    # 1) Nordic：整单批准 + 首行人工改价 → MERGE
    v = rfqs["01_nordic_excel"]
    strat = v["state"]["recommended"]
    lines = [x for x in v["state"]["quotes"][strat] if x["unit_price"]]
    ov = {lines[0]["line_key"]: round(lines[0]["unit_price"] * 0.97, 4)}
    r = step("approve nordic (MERGE+override)", lambda: ok(c.post(f"/rfq/{v['rfq_id']}/approve", json={
        "strategy": strat, "overrides": ov, "approver": "alice", "comment": "首行让价 3%"})))
    print("     committed:", len(r.get("committed", [])))

    # 2) Rhein：只 PICK 前 2 行
    v = rfqs["03_rhein_body"]
    keys = [x["line_key"] for x in v["state"]["quotes"]["balanced"] if x["unit_price"]][:2]
    r = step("approve rhein (PICK 2)", lambda: ok(c.post(f"/rfq/{v['rfq_id']}/approve", json={
        "strategy": "balanced", "line_keys": keys, "approver": "bob"})))
    print("     committed:", len(r.get("committed", [])))

    # 3) Pacific：驳回 → 丢弃分支
    v = rfqs["02_pacific_pdf"]
    step("reject pacific", lambda: ok(c.post(f"/rfq/{v['rfq_id']}/approve", json={"action": "reject"})))

    # 时间旅行重放
    rid = rfqs["01_nordic_excel"]["rfq_id"]
    rp = step("replay nordic", lambda: ok(c.get(f"/rfq/{rid}/replay")))
    print("     replay lines:", len(rp.get("lines", [])))
    audit = ok(c.get(f"/rfq/{rid}/audit"))
    print("     audit nodes:", " > ".join(a["node"] for a in audit))

    # 规则沙盒：分支 → DIFF → 回测 → 合并
    name = f"r_smoke_{int(time.time()) % 100000}"
    d = step("rule branch", lambda: ok(c.post("/rules/branches", json={"name": name,
                                                                         "changes": {"target_margin": "0.26"}})))
    print("     diff:", d["diff"]["branch"])
    bt = step("rule backtest", lambda: ok(c.get(f"/rules/branches/rule_{name}/backtest")))
    print("     total:", bt["total"])
    step("rule merge", lambda: ok(c.post(f"/rules/branches/rule_{name}/merge")))

    # 记忆：快照 → 写入 → 回滚
    n0 = len(ok(c.get("/memory")))
    step("memory snapshot", lambda: ok(c.post("/memory/snapshots", json={"name": f"m_{name}"})))
    ok(c.post("/memory", json={"content": "smoke test memory", "kind": "semantic"}))
    step("memory rollback", lambda: ok(c.post(f"/memory/rollback/m_{name}")))
    assert len(ok(c.get("/memory"))) == n0, "回滚后记忆条数不一致"

    # MemoryGate：投毒邮件 → 拦截 / 隔离分支评审合并；历史遗留投毒 → 扫描 → 影响分析 → 隔离
    v = rfqs["04_sakura_poison"]
    g = v["state"]["gate"]
    print(f"     gate: block={g['block']} review={g['review']} skip={g['skip']} inbox={g['inbox']} "
          f"risks={[r['code'] for r in v['state']['risks']['balanced'] if r['level'] == 'high']}")
    assert ok(c.get("/memory")) and len(ok(c.get("/memory"))) == n0, "投毒邮件不应改变主线记忆"
    pend = [q for q in ok(c.get("/memory/gate/queue", params={"status": "pending"})) if q["branch"] == g["inbox"]]
    res = step("gate approve (exclude high)", lambda: ok(c.post(f"/memory/gate/{g['inbox']}/approve", json={
        "reviewer": "carol", "exclude": [q["review_id"] for q in pend if q["risk"] == "high"]})))
    print(f"     approved={len(res['approved'])} rejected={res['rejected']} snapshot={res['snapshot']}")
    step("reject sakura", lambda: ok(c.post(f"/rfq/{v['rfq_id']}/approve", json={"action": "reject"})))
    mid = ok(c.post("/admin/demo/legacy-poison"))["memory_id"]
    pac = (ROOT / "samples" / "02_pacific_pdf.eml").read_bytes()
    hit = step("intake pacific (poisoned memory)", lambda: ok(c.post("/rfq", files={"eml": ("p.eml", pac)})))
    sc = step("gate scan + quarantine", lambda: ok(c.post("/memory/gate/scan", params={"apply": True})))
    f = next(x for x in sc["findings"] if x["memory_id"] == mid)
    assert any(i["rfq_id"] == hit["rfq_id"] for i in f["impact"]), "影响分析未定位到受影响报价单"
    print(f"     quarantined={len(sc['findings'])} impact={[i['rfq_id'] for i in f['impact']]} snapshot={sc['snapshot']}")
    ok(c.post(f"/rfq/{hit['rfq_id']}/approve", json={"action": "reject"}))
    print("ALL OK")


if __name__ == "__main__":
    main()
