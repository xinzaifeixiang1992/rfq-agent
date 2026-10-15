"""公网演示防护：管理接口口令 + 开关、/health 不泄露配置、写接口按 IP 限流、上游超时不返回 500。"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.workflow import RFQWorkflow

TOKEN = "t-admin-0123456789"


@pytest.fixture()
def make_client(svc):
    def _make(**overrides):
        main._state.clear()
        main._hits.clear()
        svc.settings = svc.settings.model_copy(update=overrides)
        main._state.update(svc=svc, wf=RFQWorkflow(svc))
        return TestClient(main.app)
    yield _make
    main._state.clear()
    main._hits.clear()


def test_admin_requires_switch_and_token(make_client):
    c = make_client(allow_admin_reset=True, admin_token=TOKEN)
    assert c.post("/admin/reset").status_code == 403
    assert c.post("/admin/reset", headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert c.post("/admin/demo/legacy-poison").status_code == 403

    c = make_client(allow_admin_reset=False, admin_token=TOKEN)  # 开关关闭时口令正确也拒绝
    assert c.post("/admin/reset", headers={"X-Admin-Token": TOKEN}).status_code == 403

    c = make_client(allow_admin_reset=True, admin_token="")  # 未配置口令时永远拒绝
    assert c.post("/admin/reset", headers={"X-Admin-Token": ""}).status_code == 403


def test_admin_reset_with_token(make_client):
    c = make_client(allow_admin_reset=True, admin_token=TOKEN)
    r = c.post("/admin/reset", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["customers"] > 0 and r.json()["history"] > 0
    r = c.post("/admin/demo/legacy-poison", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200 and r.json()["memory_id"]


def test_health_does_not_leak_config(make_client):
    secrets = {"mo_host": "db.internal.example", "mo_user": "acct:admin:role", "mo_password": "pw-SECRET-1",
               "memoria_api_key": "sk-memoria-SECRET", "llm_api_key": "sk-llm-SECRET", "admin_token": TOKEN}
    c = make_client(demo_mode=True, **secrets)
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) <= {"ok", "db", "vcs", "memory", "llm", "demo_mode"} and body["ok"] is True
    for v in [*secrets.values(), main._settings().sqlite_path, main._settings().memoria_url]:
        assert v not in r.text


def test_write_rate_limit_per_ip(make_client):
    c = make_client(demo_mode=True, rate_limit_writes=2, rate_limit_window=60)
    assert c.post("/memory/snapshots", json={"name": "s1"}).status_code == 200
    assert c.post("/memory/snapshots", json={"name": "s2"}).status_code == 200
    r = c.post("/memory/snapshots", json={"name": "s3"})
    assert r.status_code == 429 and "频繁" in r.json()["detail"]
    assert c.get("/memory").status_code == 200  # 读接口不计数
    # 非回环来源伪造 X-Client-IP 无效，仍按真实来源计数
    assert c.post("/memory/snapshots", json={"name": "s4"}, headers={"X-Client-IP": "1.2.3.4"}).status_code == 429


def test_rate_limit_off_outside_demo(make_client):
    c = make_client(demo_mode=False, rate_limit_writes=1)
    for i in range(3):
        assert c.post("/memory/snapshots", json={"name": f"n{i}"}).status_code == 200


def test_upstream_timeout_maps_to_503(make_client, svc, monkeypatch):
    c = make_client()

    def hang(*_a, **_k):
        raise httpx.ReadTimeout("hang")

    monkeypatch.setattr(svc.memory, "list", hang)
    r = c.get("/memory")
    assert r.status_code == 503 and "稍后重试" in r.json()["detail"]
