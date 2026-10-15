"""物料标准化：客户料号 -> 我司 SKU。精确 > 别名 > 记忆中的别名映射 > 模糊匹配。"""
from __future__ import annotations

import difflib
import re

from app.db.conn import Database
from app.memory.base import MemoryItem
from app.schemas import LineItem, MatchedItem

UNIT_SYNONYMS = {
    "pcs": ("pcs", "pc", "piece", "pieces", "unit", "units", "ea", "个", "件", "只"),
    "m": ("m", "meter", "meters", "metre", "米"),
    "set": ("set", "sets", "套"),
    "kg": ("kg", "kgs", "公斤", "千克"),
    "roll": ("roll", "rolls", "卷"),
}
_ALIAS_MEM = re.compile(r"(\S+)\s*(?:=|对应|->|maps to)\s*(?:SKU\s*)?([A-Z0-9\-]+)", re.I)


def norm_code(s: str | None) -> str:
    return re.sub(r"[\s\-_/\.]", "", (s or "").upper())


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(re.findall(r"[a-z0-9.]+|[\u4e00-\u9fff]", a)), set(re.findall(r"[a-z0-9.]+|[\u4e00-\u9fff]", b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def std_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().lower()
    return next((k for k, syns in UNIT_SYNONYMS.items() if u in syns), u)


class SkuMatcher:
    def __init__(self, db: Database) -> None:
        self.skus = db.query("SELECT sku, name, spec, unit, aliases FROM product_sku")
        self.by_code: dict[str, dict] = {}
        for s in self.skus:
            self.by_code[norm_code(s["sku"])] = s
        self.by_alias: dict[str, dict] = {}
        for s in self.skus:
            for a in (s["aliases"] or "").split("|"):
                if a.strip():
                    self.by_alias[norm_code(a)] = s

    def _result(self, it: LineItem, s: dict | None, method: str, score: float) -> MatchedItem:
        m = MatchedItem(**it.model_dump(), match_method=method, match_score=round(score, 3))
        if s:
            m.sku, m.sku_name, m.std_unit = s["sku"], s["name"], s["unit"]
        return m

    def match(self, it: LineItem, alias_memories: list[MemoryItem] = ()) -> MatchedItem:
        code = norm_code(it.part_no)
        if code and code in self.by_code:
            return self._result(it, self.by_code[code], "exact", 1.0)
        if code and code in self.by_alias:
            return self._result(it, self.by_alias[code], "alias", 0.98)
        for mem in alias_memories:
            mm = _ALIAS_MEM.search(mem.content)
            if mm and code and norm_code(mm.group(1)) == code and norm_code(mm.group(2)) in self.by_code:
                return self._result(it, self.by_code[norm_code(mm.group(2))], "memory", 0.95)
        best, score = None, 0.0
        for s in self.skus:
            cands = [norm_code(s["sku"])] + [norm_code(a) for a in (s["aliases"] or "").split("|") if a]
            sc = max((difflib.SequenceMatcher(None, code, c).ratio() for c in cands), default=0) if code else 0
            if it.description:
                desc, name = it.description.lower(), s["name"].lower()
                ds = max(difflib.SequenceMatcher(None, desc, name).ratio(), _jaccard(desc, name))
                sc = max(sc, ds * 0.9)
            if sc > score:
                best, score = s, sc
        if best and score >= 0.72:
            return self._result(it, best, "fuzzy", score)
        return self._result(it, None, "none", score)
