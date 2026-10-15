"""MatrixOne 原生 Git4Data 与仿真实现的语义一致性（需真实实例：设置 RFQ_TEST_MO=1，使用独立测试库）。"""
import os

import pytest

from app.config import Settings
from app.vcs.base import VCSConflict

pytestmark = pytest.mark.skipif(os.getenv("RFQ_TEST_MO") != "1", reason="设置 RFQ_TEST_MO=1 以连接 MatrixOne")

TEST_DB = os.getenv("RFQ_TEST_MO_DATABASE", "rfq_agent_v2_test")


@pytest.fixture(scope="module")
def mo():
    from app.db.conn import MatrixOneDatabase
    from app.db.schema import init_schema
    from app.seed import drop_all
    from app.vcs.matrixone import MatrixOneVCS
    db = MatrixOneDatabase(Settings(db_backend="matrixone", mo_database=TEST_DB))
    drop_all(db)
    init_schema(db)
    yield db, MatrixOneVCS(db)
    drop_all(db)


@pytest.fixture()
def env(mo):
    db, vcs = mo
    db.execute("DROP TABLE IF EXISTS br")
    db.execute("DELETE FROM pricing_rule")
    db.executemany("INSERT INTO pricing_rule (rule_key, rule_value, version, note) VALUES (%s, %s, %s, %s)",
                   [("a", "1", "r1", ""), ("b", "2", "r1", ""), ("c", "3", "r1", "")])
    return db, vcs


def val(db, table, key):
    r = db.query_one(f"SELECT rule_value FROM {table} WHERE rule_key=%s", (key,))
    return r["rule_value"] if r else None


def test_snapshot_read_restore(env):
    db, vcs = env
    vcs.snapshot("t_s1", "pricing_rule")
    db.execute("UPDATE pricing_rule SET rule_value='9' WHERE rule_key='a'")
    assert {r["rule_key"]: r["rule_value"] for r in vcs.read_at("pricing_rule", "t_s1")}["a"] == "1"
    vcs.restore("pricing_rule", "t_s1")
    assert val(db, "pricing_rule", "a") == "1"
    vcs.snapshot("t_s1", "pricing_rule")  # 同名重建不报错


def test_diff_is_relative_to_common_ancestor(env):
    db, vcs = env
    vcs.branch("br", "pricing_rule")
    db.execute("UPDATE br SET rule_value='10', note='中文备注' WHERE rule_key='a'")
    db.execute("INSERT INTO br VALUES ('d', '4', 'r2', '')")
    db.execute("DELETE FROM pricing_rule WHERE rule_key='c'")
    d = vcs.diff("br", "pricing_rule")
    assert (d.branch.inserted, d.branch.updated, d.branch.deleted) == (1, 1, 0)
    assert (d.target.inserted, d.target.updated, d.target.deleted) == (0, 0, 1)
    assert {(r["side"], r["op"], r["key"]) for r in d.rows} >= {("br", "UPDATE", "a"), ("br", "INSERT", "d")}
    assert any(r.get("note") == "中文备注" for r in d.rows)  # DIFF 行输出中的非 ASCII 文本
    vcs.merge("br", "pricing_rule")
    assert val(db, "pricing_rule", "a") == "10" and val(db, "pricing_rule", "d") == "4"
    assert val(db, "pricing_rule", "c") is None


@pytest.mark.parametrize("mode,expect", [("FAIL", None), ("SKIP", "main"), ("ACCEPT", "branch")])
def test_merge_conflict_modes(env, mode, expect):
    db, vcs = env
    vcs.branch("br", "pricing_rule")
    db.execute("UPDATE br SET rule_value='branch' WHERE rule_key='a'")
    db.execute("UPDATE br SET rule_value='bb' WHERE rule_key='b'")
    db.execute("UPDATE pricing_rule SET rule_value='main' WHERE rule_key='a'")
    if mode == "FAIL":
        with pytest.raises(VCSConflict):
            vcs.merge("br", "pricing_rule", "FAIL")
        assert val(db, "pricing_rule", "b") == "2"
        return
    vcs.merge("br", "pricing_rule", mode)
    assert val(db, "pricing_rule", "a") == expect
    assert val(db, "pricing_rule", "b") == "bb"


def test_pick_and_drop(env):
    db, vcs = env
    vcs.branch("br", "pricing_rule")
    db.execute("UPDATE br SET rule_value='x' WHERE rule_key IN ('a', 'b')")
    vcs.pick("br", "pricing_rule", ["a"])
    assert val(db, "pricing_rule", "a") == "x" and val(db, "pricing_rule", "b") == "2"
    vcs.drop_branch("br")
    assert not db.table_exists("br")


@pytest.fixture()
def mo_svc(mo, tmp_path):
    """整套服务跑在测试库上：记忆后端 = Git4Data（MatrixOne 原生分支/快照/RESTORE）。"""
    from app.config import get_settings
    from app.seed import seed, seed_memories
    from app.services import build_services
    base = get_settings()
    s = build_services(base.model_copy(update=dict(mo_database=TEST_DB, memory_backend="git4data", llm_base_url="",
                                                   llm_api_key="", upload_dir=str(tmp_path / "up"),
                                                   output_dir=str(tmp_path / "out"))))
    seed(s.db, history=600)
    s.db.execute("DELETE FROM mem_main")
    s.db.execute("DELETE FROM mem_snapshot_meta")
    s.db.execute("DELETE FROM memory_review")
    seed_memories(s.memory)
    return s


def test_memory_git4data_native(mo_svc):
    m = mo_svc.memory
    assert m.backend == "matrixone"
    n0 = len(m.list(1000))
    m.snapshot("t0")
    m.branch("exp")
    with m.on_branch("exp"):
        mid = m.store("分支内的新经验：中文内容", kind="semantic", customer_id="C001")
    m.store("主线在分支之后新增", kind="semantic")
    assert [(x["op"], x["memory_id"], x["content"]) for x in m.diff("exp")] == [("add", mid, "分支内的新经验：中文内容")]
    m.merge("exp")
    m.delete_branch("exp")
    assert len(m.list(1000)) == n0 + 2
    m.rollback("t0")
    assert len(m.list(1000)) == n0


def test_memory_gate_native(mo_svc, tmp_path, monkeypatch):
    from app.workflow import RFQWorkflow
    from eval import run_poison
    from scripts import make_samples as ms
    monkeypatch.setattr(run_poison, "SAMPLE", ms.poison_eml(tmp_path))
    wf = RFQWorkflow(mo_svc)
    for name, r in (("mail", run_poison.e2e_poison_mail(mo_svc, wf)), ("legacy", run_poison.e2e_legacy(mo_svc, wf))):
        assert all(r["checks"].values()), (name, r)
