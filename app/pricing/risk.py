"""风险审核规则。"""
from __future__ import annotations

from app.pricing.matcher import std_unit
from app.pricing.rules import Rules
from app.schemas import MatchedItem, PriceEvidence, QuoteLine, RFQHeader, RiskItem


def assess(header: RFQHeader, items: list[MatchedItem], lines: list[QuoteLine],
           evidence: dict[str, dict], rules: Rules, parse_notes: list[str]) -> list[RiskItem]:
    risks: list[RiskItem] = []
    min_margin = rules.get("min_margin", 0.08)
    dev_alert = rules.get("deviation_alert", 0.15)
    by_no = {i.line_no: i for i in items}

    for f in header.missing_fields:
        risks.append(RiskItem(level="medium" if f in ("currency", "incoterm") else "low", code="missing_field",
                              message=f"询价缺少字段 {f}，已按默认值处理，建议追问客户"))
    for n in parse_notes:
        if "失败" in n or "视觉" in n:
            risks.append(RiskItem(level="medium", code="parse", message=n))

    total = 0.0
    for ln in lines:
        it = by_no.get(ln.line_no)
        total += ln.unit_price * ln.qty
        if not ln.sku or not ln.unit_price:
            risks.append(RiskItem(level="high", code="unmatched", line_key=ln.line_key,
                                  message=f"第 {ln.line_no} 行未匹配到 SKU/成本：{ln.description}"))
            continue
        if it and it.match_method == "fuzzy":
            risks.append(RiskItem(level="medium", code="fuzzy_match", line_key=ln.line_key,
                                  message=f"第 {ln.line_no} 行为模糊匹配（{it.match_score:.0%}）：{it.part_no} → {ln.sku}"))
        if it and it.unit and ln.unit and std_unit(it.unit) != std_unit(ln.unit):
            risks.append(RiskItem(level="medium", code="unit_mismatch", line_key=ln.line_key,
                                  message=f"第 {ln.line_no} 行单位不一致：客户 {it.unit} / 我司 {ln.unit}"))
        if ln.margin < min_margin + 0.02:
            risks.append(RiskItem(level="high" if ln.margin <= min_margin + 1e-6 else "medium", code="low_margin",
                                  line_key=ln.line_key, message=f"第 {ln.line_no} 行毛利 {ln.margin:.1%} 接近/触及红线"))
        ev = evidence.get(ln.sku)
        if ev and ev.get("last_quote_price"):
            # 证据中的价格为人民币口径，换算比较
            last = PriceEvidence(**ev).last_quote_price
            cur_cny = ln.unit_price / max(ln.unit_cost, 1e-9) * ev["unit_cost_cny"]
            dev = (cur_cny - last) / last
            if abs(dev) > dev_alert:
                risks.append(RiskItem(level="medium", code="price_deviation", line_key=ln.line_key,
                                      message=f"第 {ln.line_no} 行较该客户上次报价偏离 {dev:+.1%}"))
        if it and it.target_price and ln.unit_price > it.target_price * 1.1:
            risks.append(RiskItem(level="low", code="above_target", line_key=ln.line_key,
                                  message=f"第 {ln.line_no} 行报价高于客户目标价 {it.target_price}"))
    if total > rules.get("big_order_amount", 100000):
        risks.append(RiskItem(level="low", code="big_order", message=f"订单金额 {total:,.0f}，建议主管复核"))
    return risks
