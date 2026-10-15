"""合成演示数据（固定随机种子，可复现）。全部为虚构数据，不含任何真实客户信息。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.db.conn import Database
from app.db.schema import init_schema

CATEGORIES = [
    ("CBL", "PVC insulated cable", "m", ["0.5mm²", "0.75mm²", "1.0mm²", "1.5mm²", "2.5mm²"],
     ["black", "red", "blue", "yellow", "green"], (0.6, 4.5)),
    ("CON", "Wire-to-board connector", "pcs", ["2P", "3P", "4P", "6P", "8P"],
     ["2.54mm", "3.96mm", "5.08mm", "2.0mm", "1.25mm"], (0.3, 3.0)),
    ("TRM", "Crimp terminal", "pcs", ["ring M3", "ring M4", "fork M3", "pin 1.0", "spade 6.3"],
     ["tin-plated", "nickel", "bare", "insulated", "gold"], (0.03, 0.4)),
    ("HSK", "Heat shrink tube", "m", ["Φ2", "Φ4", "Φ6", "Φ8", "Φ12"],
     ["2:1 black", "2:1 clear", "3:1 black", "3:1 red", "dual-wall"], (0.4, 3.5)),
    ("TIE", "Nylon cable tie", "pcs", ["2.5x100", "3.6x150", "4.8x200", "4.8x300", "7.6x400"],
     ["natural", "black UV", "releasable", "stainless", "mount head"], (0.02, 0.5)),
    ("RLY", "Automotive relay", "pcs", ["12V 20A", "12V 40A", "24V 20A", "24V 40A", "12V 70A"],
     ["SPST", "SPDT", "with diode", "with resistor", "sealed"], (4.0, 18.0)),
    ("FUS", "Blade fuse", "pcs", ["5A", "10A", "15A", "20A", "30A"],
     ["mini", "standard", "low-profile", "maxi", "micro2"], (0.15, 1.8)),
    ("SWT", "Rocker switch", "pcs", ["ON-OFF", "ON-ON", "ON-OFF-ON", "momentary", "illuminated"],
     ["2 pin", "3 pin", "4 pin", "6 pin", "IP65"], (1.5, 9.0)),
]
MFR = ("W", "TE", "MX", "PH", "AM")

CUSTOMERS = [
    ("C001", "Nordic Automation AB", "nordic-auto.se", "strategic", "EU", "T/T 30 days", 0.55),
    ("C002", "Pacific Electric Co.", "pacific-elec.com", "key", "NA", "T/T 50% deposit", 0.32),
    ("C003", "Rhein Industrietechnik GmbH", "rhein-tech.de", "key", "EU", "NET 60", 0.41),
    ("C004", "Andes Mining Supplies", "andes-mining.cl", "normal", "LATAM", "L/C at sight", 0.28),
    ("C005", "Sakura Components KK", "sakura-comp.jp", "strategic", "APAC", "T/T 30 days", 0.6),
    ("C006", "Gulf Power Systems", "gulfpower.ae", "normal", "ME", "L/C 90 days", 0.25),
    ("C007", "Maple Fleet Services", "maplefleet.ca", "normal", "NA", "NET 30", 0.35),
    ("C008", "Danube Motors d.o.o.", "danube-motors.rs", "normal", "EU", "T/T in advance", 0.3),
    ("C009", "Outback Auto Parts Pty", "outback-auto.com.au", "key", "APAC", "NET 45", 0.44),
    ("C010", "Iberia Solar SL", "iberia-solar.es", "normal", "EU", "T/T 30 days", 0.37),
    ("C011", "Nile Industrial Trading", "nile-trading.eg", "normal", "ME", "L/C at sight", 0.22),
    ("C012", "Baltic Marine OÜ", "baltic-marine.ee", "normal", "EU", "T/T 30 days", 0.4),
    ("C013", "Lion City Robotics Pte", "lioncity-robotics.sg", "key", "APAC", "NET 30", 0.47),
    ("C014", "Prairie Ag Equipment", "prairie-ag.com", "normal", "NA", "NET 60", 0.33),
    ("C015", "Alpine Rail Tech AG", "alpine-rail.ch", "strategic", "EU", "NET 30", 0.58),
    ("C016", "Savanna Telecom Ltd", "savanna-tel.co.ke", "normal", "AF", "T/T in advance", 0.2),
    ("C017", "Aurora EV Systems", "aurora-ev.no", "key", "EU", "T/T 30 days", 0.5),
    ("C018", "Mekong Electronics JSC", "mekong-elec.vn", "normal", "APAC", "T/T 30% deposit", 0.36),
    ("C019", "Atlas Heavy Industries", "atlas-heavy.tr", "normal", "EU", "L/C 60 days", 0.27),
    ("C020", "Coral Bay Yachts", "coralbay-yachts.com", "normal", "NA", "NET 30", 0.42),
]
FX = {"CNY": 1.0, "USD": 7.12, "EUR": 7.78, "GBP": 9.05, "JPY": 0.048, "HKD": 0.91}
CUR_BY_REGION = {"EU": "EUR", "NA": "USD", "APAC": "USD", "LATAM": "USD", "ME": "USD", "AF": "USD"}

RULES = {
    "version": ("r1.0", "规则集版本"),
    "target_margin": ("0.22", "目标毛利率"),
    "min_margin": ("0.08", "毛利红线"),
    "tier_factor.strategic": ("0.95", "战略客户系数"),
    "tier_factor.key": ("0.98", "重点客户系数"),
    "tier_factor.normal": ("1.0", "普通客户系数"),
    "qty_break": ("100:1.0,500:0.97,1000:0.95,5000:0.92", "数量阶梯系数"),
    "anchor_weight": ("0.4", "历史成交价锚定权重"),
    "trend_weight": ("0.5", "市场趋势权重"),
    "strategy.win_first": ("-0.04", "争单优先策略调整"),
    "strategy.balanced": ("0", "均衡策略调整"),
    "strategy.margin_first": ("0.05", "利润优先策略调整"),
    "deviation_alert": ("0.15", "与上次报价偏离预警阈值"),
    "big_order_amount": ("100000", "大额订单阈值（报价币种）"),
    "default_currency": ("USD", "缺省报价币种"),
    "memory_weight": ("0.5", "人工改价经验记忆权重"),
    "memory_adj_cap": ("0.1", "记忆调价上限"),
    "gate_override_cap": ("0.15", "MemoryGate：单次改价幅度超过此值的经验需复核"),
    "gate_discount_tol": ("0.05", "MemoryGate：折扣声明相对历史平均让价的容差"),
    "gate_price_floor": ("0.8", "MemoryGate：价格声明低于历史最低价的此比例即拦截"),
}

# 演示用记忆：客户画像 + 客户料号映射（trust T1 = 来自 CRM/人工确认）
SEED_MEMORIES = [
    ("客户 Pacific Electric 价格敏感，通常比价 3 家以上，去年两次因价格丢单", "profile", "C002", "T1", "crm"),
    ("客户 Nordic Automation 看重交期与质量，对价格不敏感，要求 RoHS 证书", "profile", "C001", "T1", "crm"),
    ("PE-CAB-15BK = SKU CBL-0016", "alias", "C002", "T1", "sales-confirmed"),
    ("NA-RLY-40 = SKU RLY-0131", "alias", "C001", "T1", "sales-confirmed"),
]


def _skus() -> list[dict]:
    out, n = [], 0
    for prefix, name, unit, specs, variants, _ in CATEGORIES:
        for s in specs:
            for v in variants:
                n += 1
                aliases = [f"{MFR[n % 5]}{1000 + n}-{v.split()[0][:2].upper()}"]
                if n % 3 == 0:
                    aliases.append(f"{prefix}{n:04d}")
                out.append({"sku": f"{prefix}-{n:04d}", "name": f"{name} {s} {v}", "spec": f"{s} {v}",
                            "unit": unit, "category": prefix, "aliases": "|".join(aliases)})
    return out


def seed(db: Database, *, history: int = 2000, rnd_seed: int = 42, models: dict | None = None) -> dict:
    rnd = random.Random(rnd_seed)
    init_schema(db)
    for t in ("customer", "product_sku", "cost_price", "quote_history", "fx_rate", "pricing_rule",
              "model_catalog", "model_route"):
        db.execute(f"DELETE FROM {t}")
    ts = datetime.now().isoformat(timespec="seconds")
    db.executemany("INSERT INTO customer VALUES (%s,%s,%s,%s,%s,%s,%s)", CUSTOMERS)

    skus = _skus()
    db.executemany("INSERT INTO product_sku VALUES (%s,%s,%s,%s,%s,%s)",
                   [(s["sku"], s["name"], s["spec"], s["unit"], s["category"], s["aliases"]) for s in skus])
    rng = {c[0]: c[5] for c in CATEGORIES}
    costs = {s["sku"]: round(rnd.uniform(*rng[s["category"]]), 4) for s in skus}
    db.executemany("INSERT INTO cost_price VALUES (%s,%s,%s,%s,%s)",
                   [(k, v, "CNY", f"SUP-{rnd.randint(1, 12):02d}", ts) for k, v in costs.items()])
    db.executemany("INSERT INTO fx_rate VALUES (%s,%s,%s)", [(k, v, ts) for k, v in FX.items()])
    db.executemany("INSERT INTO pricing_rule VALUES (%s,%s,%s,%s)",
                   [(k, v, RULES["version"][0], note) for k, (v, note) in RULES.items()])

    m = models or {"fast": "deepseek-v4.1-flash", "strong": "deepseek-v4-pro", "vision": "qwen3.8-max"}
    db.executemany("INSERT INTO model_catalog VALUES (%s,%s,%s)", [
        (m["fast"], "llm", 0.0008), (m["strong"], "llm", 0.0024), (m["vision"], "vlm", 0.003),
        ("rule-engine", "rule", 0.0)])
    db.executemany("INSERT INTO model_route VALUES (%s,%s,%s,%s,%s,%s)", [
        ("intake_extract", m["fast"], m["strong"], "rule-engine", 0.75, "v1"),
        ("body_items", m["fast"], m["strong"], "rule-engine", 0.7, "v1"),
        ("vision_items", m["vision"], m["vision"], "rule-engine", 0.6, "v1"),
        ("reply_email", m["fast"], m["strong"], "rule-engine", 0.7, "v1")])

    # 历史报价：价格随时间轻微上涨（制造可观测的趋势），约 40% 成交
    now = datetime.now()
    rows = []
    sku_ids = list(costs)
    hot = sku_ids[::7]  # 热门物料，历史更密集
    for i in range(history):
        c = rnd.choice(CUSTOMERS)
        sku = rnd.choice(hot) if rnd.random() < 0.5 else rnd.choice(sku_ids)
        days = rnd.randint(1, 365)
        cur = CUR_BY_REGION[c[4]]
        drift = 1 + 0.06 * (365 - days) / 365
        price_cny = costs[sku] * rnd.uniform(1.12, 1.42) * drift
        won = rnd.random() < c[6]
        deal = price_cny * rnd.uniform(0.95, 1.0) if won else None
        rows.append((f"H{i:05d}", c[0], sku, rnd.choice([100, 200, 500, 1000, 2000, 5000]),
                     round(price_cny / FX[cur], 4), cur, (now - timedelta(days=days)).isoformat(timespec="seconds"),
                     int(won), round(deal / FX[cur], 4) if deal else None))
    db.executemany("INSERT INTO quote_history VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    return {"customers": len(CUSTOMERS), "skus": len(skus), "history": len(rows), "rules": len(RULES)}


def seed_memories(memory) -> int:
    for content, kind, cid, trust, src in SEED_MEMORIES:
        memory.store(content, kind=kind, customer_id=cid, trust_tier=trust, source=src)
    memory.snapshot("baseline", "演示初始记忆")
    return len(SEED_MEMORIES)


def drop_all(db: Database) -> None:
    """清空业务库（含分支表/快照副本），仅用于演示重置。"""
    if db.dialect == "sqlite":
        names = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table' "
                                             "AND name NOT LIKE 'sqlite_%'")]
    else:
        from app.vcs.matrixone import MatrixOneVCS
        MatrixOneVCS(db).drop_snapshots()  # 快照为账户级对象，不随 DROP TABLE 删除
        names = [r["name"] for r in db.query("SELECT table_name AS name FROM information_schema.tables "
                                             "WHERE table_schema=%s", (db.database,))]
    for n in names:
        db.execute(f'DROP TABLE IF EXISTS "{n}"' if db.dialect == "sqlite" else f"DROP TABLE IF EXISTS `{n}`")
