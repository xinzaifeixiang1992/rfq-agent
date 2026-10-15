"""Git4Data 仿真语义：快照/分支/三方 DIFF/MERGE 冲突策略/PICK。"""
import pytest

from app.services import build_services
from app.vcs.base import VCSConflict, ident


@pytest.fixture()
def env(settings):
    s = build_services(settings)
    s.db.executemany("INSERT INTO pricing_rule (rule_key, rule_value, version, note) VALUES (%s, %s, %s, %s)",
                     [("a", "1", "r1", ""), ("b", "2", "r1", ""), ("c", "3", "r1", "")])
    return s.db, s.vcs


def val(db, table, key):
    r = db.query_one(f"SELECT rule_value FROM {table} WHERE rule_key=%s", (key,))
    return r["rule_value"] if r else None


def test_snapshot_read_restore(env):
    db, vcs = env
    vcs.snapshot("s1", "pricing_rule")
    db.execute("UPDATE pricing_rule SET rule_value='9' WHERE rule_key='a'")
    assert {r["rule_key"]: r["rule_value"] for r in vcs.read_at("pricing_rule", "s1")}["a"] == "1"
    vcs.restore("pricing_rule", "s1")
    assert val(db, "pricing_rule", "a") == "1"


def test_diff_is_relative_to_common_ancestor(env):
    db, vcs = env
    vcs.branch("br", "pricing_rule")
    db.execute("UPDATE br SET rule_value='10' WHERE rule_key='a'")
    db.execute("INSERT INTO br VALUES ('d', '4', 'r2', '')")
    db.execute("DELETE FROM pricing_rule WHERE rule_key='c'")  # 主线独立变更
    d = vcs.diff("br", "pricing_rule")
    assert (d.branch.inserted, d.branch.updated, d.branch.deleted) == (1, 1, 0)
    assert (d.target.inserted, d.target.updated, d.target.deleted) == (0, 0, 1)
    vcs.merge("br", "pricing_rule")  # 无同行冲突
    assert val(db, "pricing_rule", "a") == "10" and val(db, "pricing_rule", "d") == "4"
    assert val(db, "pricing_rule", "c") is None  # 主线的删除不被分支覆盖


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
        assert val(db, "pricing_rule", "b") == "2"  # 失败不产生部分写入
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
    assert not db.query_one("SELECT 1 AS x FROM sqlite_master WHERE name IN ('br', '_base_br')")


def test_ident_rejects_injection():
    for bad in ("a;DROP TABLE x", "1abc", "a b", "x" * 65):
        with pytest.raises(ValueError):
            ident(bad)
