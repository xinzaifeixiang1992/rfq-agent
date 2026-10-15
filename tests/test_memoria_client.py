"""Memoria 客户端协议：按官方 api-reference 的假服务校验请求格式、分页、限流重试与错误信息。"""
import httpx
import pytest

from app.memory.memoria import MemoriaError, MemoriaStore
from tests.fake_memoria import FakeMemoria


@pytest.fixture()
def m():
    s = MemoriaStore("http://memoria.test", "test-key")
    s.fake = FakeMemoria()
    s.http = httpx.Client(base_url="http://memoria.test", headers=dict(s.http.headers), transport=s.fake)
    return s


def test_protocol(m):
    mid = m.store("Nordic 偏好 DAP", kind="episodic", customer_id="C001", trust_tier="T2", source="outcome:R1")
    raw = m.fake.branches["main"][mid]
    assert raw["memory_type"] == "semantic" and raw["subject_id"] == "C001" and raw["trust_tier"] == "T2"
    assert raw["content"].startswith("[kind:episodic][customer:C001][trust:T2]")
    it = m.list(1)[0]
    assert (it.memory_type, it.customer_id, it.trust_tier, it.source) == ("episodic", "C001", "T2", "outcome:R1")
    for i in range(230):
        m.store(f"m{i}")
    assert len(m.list(1000)) == 231 and len(m.list(150)) == 150  # cursor 分页，单页 ≤100
    assert [i.memory_id for i in m.retrieve("Nordic DAP", "C001", kinds=["episodic"])] == [mid]
    m.forget(mid, reason="quarantine")
    assert mid not in m.fake.branches["main"]
    m.governance()
    m.consolidate(force=True)
    with pytest.raises(MemoriaError) as e:
        m.rollback("nope")
    assert e.value.status == 404 and "snapshot not found" in str(e.value)


def test_auth_error():
    s = MemoriaStore("http://memoria.test", "bad")
    s.http = httpx.Client(base_url="http://memoria.test", headers=dict(s.http.headers), transport=FakeMemoria())
    with pytest.raises(MemoriaError) as e:
        s.list(1)
    assert e.value.status == 401


def test_snapshot_list_timeout_falls_back(m):
    m.snapshot("baseline")
    m.fake.slow_snaps.add("baseline")
    assert [s["name"] for s in m.snapshots()] == ["baseline"]  # 整页与逐条都超时，返回本进程已知列表
    assert [s["name"] for s in m.snapshots()] == ["baseline"]  # 退避期内不再请求远端
    m.snapshot("pre_merge_x")
    assert {s["name"] for s in m.snapshots()} == {"baseline", "pre_merge_x"}


def test_snapshot_limit_recycles_owned_snapshots(m):
    m.fake.snap_limit = 5
    m.RECYCLE_BATCH = 2
    m.snapshot("baseline")  # 非本应用快照不得删除
    for i in range(4):
        m.snapshot(f"m_rfq{i}")
    m.fake.slow_snaps.add("m_rfq1")  # 一个慢条目不能阻塞回收其他可识别快照

    assert m.snapshot("m_rfq_new") == "m_rfq_new"
    assert "baseline" in m.fake.snaps and "m_rfq_new" in m.fake.snaps
    assert len(m.fake.snaps) == 4  # 批量回收 2 个后新建 1 个
