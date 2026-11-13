"""数据核验：对 SQLite 离线库与 MatrixOne 线上库跑同一套核验项，输出每个数字的出处。

用法（在仓库根目录，用虚拟环境解释器）：
    .\\.venv\\Scripts\\python.exe scripts\\verify_data.py                      # 用 .env 里的 DB_BACKEND
    .\\.venv\\Scripts\\python.exe scripts\\verify_data.py --db sqlite --path data/rfq_offline.db
    .\\.venv\\Scripts\\python.exe scripts\\verify_data.py --db matrixone
    .\\.venv\\Scripts\\python.exe scripts\\verify_data.py --rfq RFQ202611130949295726
    .\\.venv\\Scripts\\python.exe scripts\\verify_data.py --sql "select * from pricing_rule"

只读，不写任何数据。每个核验项标题后面括号里是它对应的表名，便于现场回答“这个数从哪来”。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台默认 GBK：不改编码（改了反而乱码），只把不可编码字符兜底成 ?
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:  # 已被重定向为不可重配置的流时忽略
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db.conn import Database, MatrixOneDatabase, SQLiteDatabase  # noqa: E402

STRATEGIES = ("win_first", "balanced", "margin_first")
KEY_RULES = ("target_margin", "min_margin", "anchor_weight", "memory_weight",
             "strategy.win_first", "strategy.balanced", "strategy.margin_first")
BASE_TABLES = ("customer", "product_sku", "cost_price", "quote_history",
               "pricing_rule", "fx_rate", "model_route", "model_catalog")


# ---------------------------------------------------------------- 输出
def title(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def show(rows: list[dict], cols: list[str] | None = None, limit: int = 25) -> None:
    if not rows:
        print("  (空)")
        return
    cols = cols or list(rows[0].keys())

    def cell(v) -> str:
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:.6f}".rstrip("0").rstrip(".")
        return str(v)[:36]

    width = [max([len(c)] + [len(cell(r.get(c))) for r in rows[:limit]]) for c in cols]
    print("  " + "  ".join(c.ljust(width[i]) for i, c in enumerate(cols)))
    print("  " + "  ".join("-" * w for w in width))
    for r in rows[:limit]:
        print("  " + "  ".join(cell(r.get(c)).ljust(width[i]) for i, c in enumerate(cols)))
    if len(rows) > limit:
        print(f"  ... 共 {len(rows)} 行，仅显示前 {limit} 行")


def has(db: Database, table: str) -> bool:
    return db.table_exists(table)


def table_names(db: Database) -> list[str]:
    if db.dialect == "sqlite":
        return [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")]
    return [str(list(r.values())[0]) for r in db.query("SHOW TABLES")]


# ---------------------------------------------------------------- 核验项
def check_conn(db: Database, args) -> None:
    names = table_names(db)
    title(f"① 连接与库对象（{args.db}）")
    if db.dialect == "matrixone":
        ver = db.query("SELECT version() AS v")[0]["v"]
        print(f"  MatrixOne  {args.mo_host}:{args.mo_port}  库={args.mo_database}  版本={ver}")
    else:
        print(f"  SQLite     {args.path}")
    print(f"  表数量     {len(names)}")
    print(f"  ql_* 分支表 {len([n for n in names if n.startswith('ql_')])} 个")
    print(f"  快照对象     {len([n for n in names if n.startswith(('_snap_', '_vcsbase_'))])} 个")


def check_baseline(db: Database) -> None:
    title("② 主数据基线（customer / product_sku / cost_price / quote_history / pricing_rule / fx_rate）")
    rows = []
    for t in BASE_TABLES:
        if has(db, t):
            rows.append({"table": t, "rows": db.query(f"SELECT count(*) AS c FROM {t}")[0]["c"]})
    show(rows)


def check_rules(db: Database) -> None:
    title("③ 定价参数（pricing_rule）—— PPT 上 -4% / 0 / +5% 的唯一出处")
    rows = db.query(
        "SELECT rule_key, rule_value, version, note FROM pricing_rule WHERE rule_key IN (%s)"
        % ",".join(["%s"] * len(KEY_RULES)), KEY_RULES)
    order = {k: i for i, k in enumerate(KEY_RULES)}
    show(sorted(rows, key=lambda r: order.get(r["rule_key"], 99)))
    total = db.query("SELECT count(*) AS c FROM pricing_rule")[0]["c"]
    print(f"  （pricing_rule 共 {total} 条，此处只显示与报价直接相关的 {len(KEY_RULES)} 条）")


def check_rfqs(db: Database, args, limit: int = 10) -> list[str]:
    title("④ 询价单（rfq_record）")
    sql = "SELECT rfq_id, customer_id, status, data_snapshot, memory_snapshot, created_at FROM rfq_record ORDER BY created_at DESC"
    if args.rfq:
        sql = ("SELECT rfq_id, customer_id, status, data_snapshot, memory_snapshot, created_at "
               f"FROM rfq_record WHERE rfq_id = '{args.rfq}'")
    rows = db.query(sql)
    show(rows, limit=limit)
    return [r["rfq_id"] for r in rows[:3]]


def _quote_rows(db: Database, rfq_id: str, strategy: str) -> list[dict]:
    """三档报价：优先读 Git4Data 分支表 ql_<rfq>_<策略>，回落到 quote_line。"""
    branch = f"ql_{rfq_id.lower()}_{strategy}"
    columns = "sku, qty, unit_cost, unit_price, margin, currency"
    if has(db, branch):
        return db.query(f"SELECT {columns} FROM {branch}")
    if has(db, "quote_line"):
        return db.query(f"SELECT {columns} FROM quote_line WHERE rfq_id = %s AND strategy = %s",
                        (rfq_id, strategy))
    return []


def check_quotes(db: Database, rfq_id: str, sku: str | None = None) -> None:
    title(f"⑤ 三档报价（ql_{rfq_id.lower()}_<策略>）—— 整单金额与加权毛利率")
    rows, detail = [], {}
    for st in STRATEGIES:
        items = _quote_rows(db, rfq_id, st)
        detail[st] = items
        if not items:
            rows.append({"strategy": st, "lines": 0, "amount": None, "wmargin": None, "vs_balanced": None})
            continue
        amount = sum(float(r["qty"]) * float(r["unit_price"]) for r in items)
        cost = sum(float(r["qty"]) * float(r["unit_cost"]) for r in items)
        rows.append({"strategy": st, "lines": len(items), "amount": round(amount, 2),
                     "wmargin": round((amount - cost) / amount, 4) if amount else None})
    base = next((r["amount"] for r in rows if r["strategy"] == "balanced" and r["amount"]), None)
    for r in rows:
        r["vs_balanced"] = round(r["amount"] / base - 1, 4) if base and r["amount"] else None
    show(rows)
    if base:
        print(f"  说明：vs_balanced 是整单实差；策略参数本身是 -4% / 0 / +5%，"
              f"差额来自毛利红线 min_margin 把触底行抬回。")

    if not any(detail.values()):
        return
    sku = sku or detail["balanced"][0]["sku"]
    title(f"⑥ 单行复核（{sku}）—— 毛利率与毛利红线的算法")
    out = []
    for st in STRATEGIES:
        row = next((r for r in detail[st] if r["sku"] == sku), None)
        if not row:
            continue
        price, cost = float(row["unit_price"]), float(row["unit_cost"])
        out.append({"strategy": st, "unit_cost": cost, "unit_price": price,
                    "margin_stored": float(row["margin"] or 0),
                    "margin_recalc": round((price - cost) / price, 4),
                    "qty": row["qty"], "currency": row["currency"]})
    show(out)
    print("  算法：毛利率 = (报价 - 成本) / 报价。margin_stored 是落库值（用未取整报价算出），"
          "margin_recalc 用落库的 4 位小数报价重算，所以小额行会有 0.x 个百分点的舍入差。")
    if sku == "CBL-0016":
        print("  PPT 上的 12.0% 即该行 balanced 档的 margin；7.4% 是被污染后触底到 min_margin=0.08 的值，"
              "恒等于 0.08 / 1.08。")


def check_decisions(db: Database, rfq_id: str | None) -> None:
    title("⑦ 决策日志（decision_log）—— 延迟 / token / 成本 / 置信度")
    where, params = ("", ())
    if rfq_id:
        where, params = (" WHERE rfq_id = %s", (rfq_id,))
    rows = db.query(
        "SELECT node, count(*) AS n, round(avg(latency_ms),1) AS avg_ms, sum(tokens) AS tokens, "
        "round(sum(cost),6) AS cost, round(avg(confidence),4) AS avg_conf "
        f"FROM decision_log{where} GROUP BY node ORDER BY n DESC, node", params)
    show(rows)
    if rfq_id and has(db, "decision_log"):
        cols = ("node", "model", "confidence", "latency_ms", "tokens", "cost",
                "data_snapshot", "memory_snapshot", "input_digest", "human_action")
        detail = db.query(f"SELECT {', '.join(cols)} FROM decision_log WHERE rfq_id = %s "
                          "ORDER BY created_at", (rfq_id,))
        if detail:
            title(f"    该单逐节点明细（{rfq_id}）")
            show(detail, list(cols), limit=12)


def check_gate(db: Database) -> None:
    title("⑧ 记忆网关（memory_review）—— PPT “100% / 0%” 的出处")
    if not has(db, "memory_review"):
        print("  (该库无 memory_review 表，记忆后端可能为 memoria 云)")
        return
    rows = db.query("SELECT decision, status, count(*) AS n FROM memory_review "
                    "GROUP BY decision, status ORDER BY n DESC")
    show(rows)
    blocked = sum(r["n"] for r in rows if r["decision"] == "block")
    total = sum(r["n"] for r in rows)
    print(f"  拦截 {blocked} / 共 {total} 条候选。")


def check_vcs(db: Database) -> None:
    title("⑨ 版本对象（Git4Data）—— 分支 / 快照 / 共同祖先")
    if db.dialect == "sqlite":
        if has(db, "_vcs_branch"):
            show(db.query("SELECT name, source, created_at FROM _vcs_branch ORDER BY created_at"), limit=12)
        if has(db, "_vcs_snapshot"):
            show(db.query("SELECT name, table_name, copy_table, created_at FROM _vcs_snapshot "
                          "ORDER BY created_at"), limit=12)
        print("  SQLite 下由 _vcs_branch / _vcs_snapshot + _snap_* 副本表模拟；MatrixOne 下为原生对象。")
        return
    rows = []
    for r in db.query("SHOW SNAPSHOTS"):
        row = {str(k).lower(): v for k, v in r.items()}
        if (row.get("database_name") or row.get("database")) != db.database:
            continue
        rows.append({"snapshot": row.get("snapshot_name") or row.get("name"),
                     "table": row.get("table_name"),
                     "created": row.get("created_time") or row.get("create_time")})
    show(rows, limit=12)
    print("  原生命令：SHOW SNAPSHOTS / CREATE SNAPSHOT <n> FOR TABLE <db> <t> / "
          "RESTORE TABLE <db>.<t> {SNAPSHOT = <n>} / DATA BRANCH DIFF <b> AGAINST <t>")


# ---------------------------------------------------------------- 入口
def main() -> None:
    ap = argparse.ArgumentParser(description="RFQ 数据核验（只读）")
    ap.add_argument("--db", choices=("sqlite", "matrixone"), help="默认取 .env 的 DB_BACKEND")
    ap.add_argument("--path", default="data/rfq_offline.db", help="SQLite 库路径（默认离线演示库）")
    ap.add_argument("--rfq", help="指定询价单号；不填则取最新一单")
    ap.add_argument("--sku", help="单行复核的料号；不填则取该单 balanced 档第一行")
    ap.add_argument("--sql", help="执行一条自定义只读 SQL 后退出")
    args = ap.parse_args()

    s = get_settings()
    args.db = args.db or s.db_backend
    args.mo_host, args.mo_port, args.mo_database = s.mo_host, s.mo_port, s.mo_database

    if args.db == "sqlite":
        path = Path(args.path)
        path = path if path.is_absolute() else ROOT / path
        if not path.exists():
            sys.exit(f"SQLite 库不存在：{path}")
        db: Database = SQLiteDatabase(str(path))
    else:
        if not (s.mo_host and s.mo_user):
            sys.exit("请先在 .env 中填写 MO_HOST / MO_USER / MO_PASSWORD")
        db = MatrixOneDatabase(s)
        if not has(db, "rfq_record"):
            sys.exit(f"库 {s.mo_database} 中还没有 rfq_record，先跑一次 /admin/reset 或 scripts/seed.py")

    if args.sql:
        title(f"自定义查询：{args.sql}")
        show(db.query(args.sql), limit=100)
        return

    print(f"# RFQ 数据核验报告　库={args.db}　{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}")
    check_conn(db, args)
    check_baseline(db)
    check_rules(db)
    rfqs = check_rfqs(db, args)
    target = args.rfq or (rfqs[0] if rfqs else None)
    if target:
        check_quotes(db, target, args.sku)
        check_decisions(db, target)
    check_gate(db)
    check_vcs(db)
    print("\n完成。所有数字均可由上述表直接复现，SQL 与 PPT 页面一一对应。")


if __name__ == "__main__":
    main()
