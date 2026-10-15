"""端到端：SQLite 仿真 Git4Data + 本地记忆 + 规则引擎（无 LLM）。"""
from app.workflow import RFQWorkflow

BODY = """Dear sales,

Please quote the following items, FOB Shanghai, USD, delivery within 4 weeks.

PE-CAB-15BK PVC cable black: 2000 m
PH1003-BL cable: 800 m
CBL-0001 PVC cable: 300 m
XYZ-77777 special sensor: 50 pcs

Best regards,
John Smith
Pacific Electric Co.
"""


def _start(wf):
    return wf.start(subject="RFQ - harness components", sender="John <john@pacific-elec.com>", body=BODY)


def test_quote_as_commit_flow(svc):
    wf = RFQWorkflow(svc)
    v = _start(wf)
    rfq = v["rfq_id"]
    st = v["state"]
    assert v["pending"] and v["record"]["status"] == "pending_approval"
    assert st["customer"]["customer_id"] == "C002"
    assert st["header"]["incoterm"] == "FOB" and st["header"]["destination"] == "Shanghai"
    methods = [m["match_method"] for m in st["matched"]]
    assert methods[:3] == ["memory", "alias", "exact"] and methods[3] == "none"
    assert st["recommended"] == "win_first"  # 客户画像记忆：价格敏感
    for s, b in st["branches"].items():
        assert svc.db.table_exists(b)
        assert st["branch_diffs"][s]["inserted"] == 4
    p = {s: st["quotes"][s][0]["unit_price"] for s in st["quotes"]}
    assert p["win_first"] < p["balanced"] < p["margin_first"]
    assert any(r["code"] == "unmatched" for r in st["risks"]["balanced"])

    key = f"{rfq}:1"
    new_price = round(p["win_first"] * 0.95, 4)
    v = wf.resume(rfq, {"action": "approve", "strategy": "win_first", "overrides": {key: new_price},
                        "approver": "alice", "comment": "客户比价压价"})
    assert v["record"]["status"] == "quoted" and not v["pending"]
    committed = {c["line_key"]: c for c in v["committed"]}
    assert len(committed) == 3 and f"{rfq}:4" not in committed  # 未定价行未进入主线（PICK）
    assert committed[key]["unit_price"] == new_price
    assert not any(svc.db.table_exists(b) for b in st["branches"].values())
    files = v["state"]["outputs"]["files"]
    assert all(__import__("os").path.exists(files[k]) for k in ("xlsx", "pdf", "html", "reply"))

    audit = svc.audit.list(rfq)
    nodes = [a["node"] for a in audit]
    assert nodes == ["intake", "parse", "memory_gate", "normalize", "price", "risk", "approval", "commit", "render"]
    by = {a["node"]: a for a in audit}
    assert by["approval"]["human_action"] == "approve"
    assert by["commit"]["human_action"] == "commit:pick"
    assert by["intake"]["data_snapshot"] == f"sn_{rfq.lower()}"
    assert by["intake"]["model"] == "rule-engine"

    # 记忆闭环：人工改价经验影响下一单
    v2 = _start(wf)
    line = v2["state"]["quotes"]["win_first"][0]
    assert "记忆中的人工改价经验" in line["rationale"]
    assert line["unit_price"] < p["win_first"]

    # 记忆回滚到基线：经验被撤销，下一单恢复原价
    wf.resume(v2["rfq_id"], {"action": "reject"})
    svc.memory.rollback("baseline")
    v3 = _start(wf)
    assert v3["state"]["quotes"]["win_first"][0]["unit_price"] == p["win_first"]
    wf.resume(v3["rfq_id"], {"action": "reject"})

    # 时间旅行：成本上涨后按快照重放
    svc.db.execute("UPDATE cost_price SET unit_cost = unit_cost * 1.2 WHERE sku='CBL-0001'")
    svc.db.execute("UPDATE pricing_rule SET rule_value='0.10' WHERE rule_key='min_margin'")
    rp = wf.replay(rfq)
    ln = next(x for x in rp["lines"] if x["sku"] == "CBL-0001")
    assert ln["cost_now"] > ln["cost_then"] and ln["delta_pct"] > 0
    assert rp["rule_diff"] == [{"rule_key": "min_margin", "then": "0.08", "now": "0.10"}]

    # 成交反馈：经验写入记忆分支，评审后合并
    out = wf.record_outcome(rfq, won=True, note="价格压到位后成交")
    assert out["status"] == "won" and len(out["memory_diff"]) == 1
    svc.memory.merge(out["memory_branch"])
    assert any("成交" in m.content for m in svc.memory.list())


def test_reject_discards_branches(svc):
    wf = RFQWorkflow(svc)
    v = _start(wf)
    v = wf.resume(v["rfq_id"], {"action": "reject", "comment": "不报"})
    assert v["record"]["status"] == "rejected" and v["committed"] == []
    assert not any(svc.db.table_exists(b) for b in v["state"]["branches"].values())
