"""策略定价引擎：成本 × 目标毛利 × 客户系数 × 阶梯系数 × 趋势系数，锚定历史成交价，守住毛利红线。"""
from __future__ import annotations

import math

from app.db.conn import Database
from app.pricing.rules import Rules
from app.schemas import STRATEGY_LABEL, MatchedItem, PriceEvidence, QuoteLine


class PricingEngine:
    def __init__(self, db: Database, rules: Rules, costs: list[dict] | None = None) -> None:
        """costs：可传入某个数据快照下的成本行，用于按历史快照重放报价。"""
        self.db = db
        self.rules = rules
        self.costs = {c["sku"]: c for c in costs} if costs is not None else None
        self.fx = {r["currency"]: float(r["rate_to_cny"]) for r in db.query("SELECT currency, rate_to_cny FROM fx_rate")}
        self.fx.setdefault("CNY", 1.0)

    def to_cny(self, v: float, cur: str) -> float:
        return v * self.fx.get(cur, 1.0)

    def from_cny(self, v: float, cur: str) -> float:
        return v / self.fx.get(cur, 1.0)

    def evidence(self, sku: str, customer_id: str | None) -> PriceEvidence | None:
        cost = self.costs.get(sku) if self.costs is not None else \
            self.db.query_one("SELECT unit_cost, currency FROM cost_price WHERE sku=%s", (sku,))
        if not cost:
            return None
        ev = PriceEvidence(sku=sku, unit_cost_cny=self.to_cny(float(cost["unit_cost"]), cost["currency"]))
        hist = self.db.query(
            "SELECT id, unit_price, currency, won, deal_price, quoted_at FROM quote_history "
            "WHERE sku=%s AND customer_id=%s ORDER BY quoted_at DESC LIMIT 10", (sku, customer_id or ""))
        if hist:
            ev.history_ids = [h["id"] for h in hist]
            ev.last_quote_price = self.to_cny(float(hist[0]["unit_price"]), hist[0]["currency"])
            won = [self.to_cny(float(h["deal_price"] or h["unit_price"]), h["currency"]) for h in hist if h["won"]]
            if won:
                ev.last_won_price, ev.avg_won_price = won[0], sum(won) / len(won)
        # 全客户维度的近期趋势：最近 5 次 vs 更早 5 次
        allh = self.db.query("SELECT unit_price, currency FROM quote_history WHERE sku=%s "
                             "ORDER BY quoted_at DESC LIMIT 10", (sku,))
        if len(allh) >= 6:
            p = [self.to_cny(float(h["unit_price"]), h["currency"]) for h in allh]
            recent, early = sum(p[:5]) / 5, sum(p[5:]) / len(p[5:])
            ev.trend_pct = round((recent - early) / early, 4) if early else 0.0
        return ev

    def price(self, rfq_id: str, item: MatchedItem, ev: PriceEvidence | None, customer: dict | None,
              strategy: str, currency: str, mem_adj: float = 0.0) -> QuoteLine:
        """mem_adj：来自记忆的人工改价经验（相对调整比例），已在调用方按权重与上限处理。"""
        r = self.rules
        key = f"{rfq_id}:{item.line_no}"
        base_line = dict(line_key=key, rfq_id=rfq_id, line_no=item.line_no, sku=item.sku,
                         description=item.sku_name or item.description, qty=item.qty, unit=item.std_unit or item.unit,
                         currency=currency, strategy=strategy)
        if not ev:
            return QuoteLine(**base_line, unit_cost=0, unit_price=0, margin=0, win_prob=0,
                             rationale="未匹配到成本数据，需人工报价")
        tier = (customer or {}).get("tier", "normal")
        cost = ev.unit_cost_cny
        base = cost * (1 + r.get("target_margin", 0.22)) * r.get(f"tier_factor.{tier}", 1.0) * r.qty_factor(item.qty)
        why = [f"成本 ¥{cost:.2f}，目标毛利 {r.get('target_margin', 0.22):.0%}，{tier} 客户系数，阶梯系数 {r.qty_factor(item.qty)}"]
        if ev.last_won_price:
            w = r.get("anchor_weight", 0.5)
            base = (1 - w) * base + w * ev.last_won_price
            why.append(f"锚定该客户上次成交价 ¥{ev.last_won_price:.2f}（权重 {w:.0%}）")
        if ev.trend_pct:
            base *= 1 + r.get("trend_weight", 0.5) * ev.trend_pct
            why.append(f"近期市场报价趋势 {ev.trend_pct:+.1%}")
        if mem_adj:
            base *= 1 + mem_adj
            why.append(f"记忆中的人工改价经验 {mem_adj:+.1%}")
        price = base * (1 + r.get(f"strategy.{strategy}", 0.0))
        floor = cost * (1 + r.get("min_margin", 0.08))
        if price < floor:
            price = floor
            why.append(f"触及毛利红线 {r.get('min_margin', 0.08):.0%}，已上调至底价")
        ref = ev.last_won_price or ev.avg_won_price or base
        p_price = 1 / (1 + math.exp(12 * (price / ref - 1)))
        win = 0.7 * p_price + 0.3 * float((customer or {}).get("win_rate") or 0.5)
        why.insert(0, f"【{STRATEGY_LABEL.get(strategy, strategy)}】")
        return QuoteLine(**base_line, unit_cost=round(self.from_cny(cost, currency), 4),
                         unit_price=round(self.from_cny(price, currency), 4),
                         margin=round((price - cost) / price, 4), win_prob=round(win, 3), rationale="；".join(why))
