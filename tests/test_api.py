"""API 冒烟：上传 eml → 审批 → 下载文件 → 审计/重放；规则沙盒 分支→DIFF→回测→合并；记忆快照与回滚。"""
import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.workflow import RFQWorkflow
from scripts import make_samples as ms


@pytest.fixture()
def client(svc, tmp_path):
    main._state.clear()
    main._state.update(svc=svc, wf=RFQWorkflow(svc))
    ms.make_excel(tmp_path / "a.xlsx")
    ms.eml(tmp_path / "a.eml", "Erik <erik.lund@nordic-auto.se>", "RFQ NA-2026-118",
           "Terms CIF Gothenburg, currency EUR, delivery within 6 weeks. Payment T/T.", [tmp_path / "a.xlsx"])
    with TestClient(main.app) as c:
        c.eml = (tmp_path / "a.eml").read_bytes()
        yield c
    main._state.clear()


def test_rfq_lifecycle(client):
    assert client.get("/health").json()["vcs"] == "sqlite-sim"
    r = client.post("/rfq", files={"eml": ("a.eml", client.eml, "message/rfc822")})
    assert r.status_code == 200, r.text
    rfq = r.json()["rfq_id"]
    v = client.get(f"/rfq/{rfq}").json()
    assert v["pending"] and v["state"]["recommended"] == "margin_first"  # 画像：不敏感、看重交期质量
    assert len(v["state"]["matched"]) == 6
    assert client.get(f"/rfq/{rfq}/compare").json()

    bad = client.post(f"/rfq/{rfq}/approve", json={"action": "approve", "strategy": "nope"})
    assert bad.status_code == 422
    keys = [l["line_key"] for l in v["state"]["quotes"]["margin_first"] if l["unit_price"]]
    r = client.post(f"/rfq/{rfq}/approve", json={"strategy": "margin_first", "line_keys": keys[:3], "approver": "bob"})
    assert r.status_code == 200, r.text
    assert len(r.json()["committed"]) == 3
    assert client.post(f"/rfq/{rfq}/approve", json={}).status_code == 400  # 已提交，不可重复审批

    for kind in ("xlsx", "pdf", "html"):
        assert client.get(f"/rfq/{rfq}/files/{kind}").status_code == 200
    assert client.get(f"/rfq/{rfq}/files/exe").status_code == 404
    audit = client.get(f"/rfq/{rfq}/audit").json()
    assert audit[-1]["node"] == "render" and any(a["human_action"] == "commit:pick" for a in audit)
    assert client.get(f"/rfq/{rfq}/replay").json()["lines"]
    assert client.get("/rfq/NOPE/replay").status_code == 404
    assert client.get("/audit/stats").json() and client.get("/routes").json()


def test_rule_sandbox(client):
    rfq = client.post("/rfq", files={"eml": ("a.eml", client.eml, "message/rfc822")}).json()["rfq_id"]
    r = client.post("/rules/branches", json={"name": "r2_margin", "changes": {"target_margin": "0.26"}})
    assert r.status_code == 200 and r.json()["diff"]["branch"]["updated"] == 2  # target_margin + version
    assert client.post("/rules/branches", json={"name": "x;drop", "changes": {}}).status_code == 422
    rows = client.get("/rules/branches/rule_r2_margin/diff").json()["rows"]
    assert {x["key"] for x in rows} == {"target_margin", "version"}
    bt = client.get("/rules/branches/rule_r2_margin/backtest").json()
    assert bt["rfqs"][0]["rfq_id"] == rfq and bt["total"]["branch"]["amount"] > bt["total"]["main"]["amount"]
    assert client.post("/rules/branches/rule_r2_margin/merge").status_code == 200
    rules = {x["rule_key"]: x["rule_value"] for x in client.get("/rules").json()}
    assert rules["target_margin"] == "0.26" and rules["version"] == "r2_margin"


def test_memory_snapshot_rollback(client):
    n0 = len(client.get("/memory").json())
    client.post("/memory/snapshots", json={"name": "before_test"})
    mid = client.post("/memory", json={"content": "Rhein 偏好 DAP 交付", "kind": "profile",
                                       "customer_id": "C003"}).json()["memory_id"]
    assert mid and len(client.get("/memory").json()) == n0 + 1
    assert client.get("/memory/search", params={"q": "Rhein DAP", "customer_id": "C003"}).json()
    assert client.post("/memory/rollback/before_test").status_code == 200
    assert len(client.get("/memory").json()) == n0
