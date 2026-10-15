"""MatrixOne 原生 DIFF 超时后的共同祖先降级，不依赖真实实例。"""
from app.vcs.matrixone import MatrixOneVCS


class FakeDB:
    diff_timeout = 1

    def __init__(self):
        base = [
            {"rule_key": "a", "rule_value": "1"},
            {"rule_key": "b", "rule_value": "2"},
            {"rule_key": "c", "rule_value": "3"},
        ]
        self.tables = {
            "_vcsbase_br": base,
            "br": [{"rule_key": "a", "rule_value": "10"}, base[1], base[2],
                   {"rule_key": "d", "rule_value": "4"}],
            "pricing_rule": [base[0], base[1]],
        }
        self.native_calls = 0

    def query_timeout(self, *_args, **_kwargs):
        self.native_calls += 1
        raise TimeoutError("DATA BRANCH DIFF timed out")

    def table_exists(self, name):
        return name in self.tables

    def query(self, sql, _params=()):
        return self.tables[sql.removeprefix("SELECT * FROM ")]


def test_diff_timeout_uses_common_ancestor_and_circuit_breaker():
    db = FakeDB()
    vcs = MatrixOneVCS(db)

    result = vcs.diff("br", "pricing_rule")
    assert (result.branch.inserted, result.branch.updated, result.branch.deleted) == (1, 1, 0)
    assert (result.target.inserted, result.target.updated, result.target.deleted) == (0, 0, 1)
    assert {(row["side"], row["op"], row["key"]) for row in result.rows} >= {
        ("br", "UPDATE", "a"), ("br", "INSERT", "d"), ("pricing_rule", "DELETE", "c")}

    vcs.diff("br", "pricing_rule")
    assert db.native_calls == 1  # 退避期内不再把每个策略分支都等到超时
