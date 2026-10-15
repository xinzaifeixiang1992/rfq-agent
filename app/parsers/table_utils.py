"""表头识别与行记录映射，Excel / PDF / CSV 共用。"""
from __future__ import annotations

import re
from typing import Any, Sequence

from app.schemas import LineItem

HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "line_no": ("no", "no.", "item no", "item", "line", "#", "序号", "行号"),
    "part_no": ("part no", "part number", "p/n", "pn", "model", "item code", "mpn", "型号", "料号",
                "物料编码", "规格型号", "零件号"),
    "description": ("description", "desc", "product", "product name", "品名", "名称", "描述", "物料描述"),
    "qty": ("qty", "quantity", "q'ty", "qty.", "order qty", "数量", "需求数量", "采购数量"),
    "unit": ("unit", "uom", "units", "单位"),
    "target_price": ("target price", "target", "unit price", "目标价", "单价", "期望单价"),
}
_STOP_WORDS = ("total", "subtotal", "合计", "总计", "小计")


def norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def parse_number(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def _header_field(text: str) -> str | None:
    """单元格 -> 字段：精确命中优先，其次取最长的子串命中（避免 unit 抢走 unit price）。"""
    best, score = None, 0
    for field, syns in HEADER_SYNONYMS.items():
        for s in syns:
            if text == s:
                return field
            if (len(s) > 3 or re.search(r"[\u4e00-\u9fff]", s)) and s in text and len(s) > score:
                best, score = field, len(s)
    return best


def map_header(cells: Sequence[Any]) -> dict[str, int] | None:
    """识别表头行：至少命中 2 个字段且包含数量列。"""
    found: dict[str, int] = {}
    for idx, c in enumerate(cells):
        t = norm(c).lower().rstrip(":：")
        if t and (f := _header_field(t)) and f not in found:
            found[f] = idx
    if len(found) >= 2 and "qty" in found:
        return found
    return None


def row_to_item(row: Sequence[Any], hmap: dict[str, int], line_no: int, source: str) -> LineItem | None:
    def cell(field: str) -> Any:
        i = hmap.get(field)
        return row[i] if i is not None and i < len(row) else None

    text = " ".join(norm(c) for c in row).lower()
    if not text or any(text.startswith(w) for w in _STOP_WORDS):
        return None
    qty = parse_number(cell("qty"))
    part, desc = norm(cell("part_no")) or None, norm(cell("description"))
    if not qty or not (part or desc):
        return None
    unit = norm(cell("unit")) or None
    if not unit:
        m = re.search(r"\d\s*([a-zA-Z\u4e00-\u9fff]+)$", norm(cell("qty")))
        unit = m.group(1) if m else None
    return LineItem(line_no=line_no, part_no=part, description=desc, qty=qty, unit=unit,
                    target_price=parse_number(cell("target_price")), source=source)


def is_continuation(row: Sequence[Any], hmap: dict[str, int]) -> bool:
    """只有描述有内容、数量为空：说明是上一行描述断行（常见于跨页/自动换行）。"""
    qi, di = hmap.get("qty"), hmap.get("description")
    has_desc = di is not None and di < len(row) and norm(row[di])
    no_qty = qi is None or qi >= len(row) or parse_number(row[qi]) is None
    others = [norm(c) for i, c in enumerate(row) if i != di and norm(c)]
    return bool(has_desc and no_qty and not others)


def table_to_items(rows: list[Sequence[Any]], source: str, hmap: dict[str, int] | None = None,
                   start_line: int = 1, tail: LineItem | None = None
                   ) -> tuple[list[LineItem], dict[str, int] | None]:
    """解析一张表。传入 hmap 表示续表（沿用上一页表头），tail 为上一页最后一条（用于跨页断行续写）。"""
    items: list[LineItem] = []
    body = rows
    if hmap is None:
        for i, r in enumerate(rows[:30]):
            if (h := map_header(r)):
                hmap, body = h, rows[i + 1:]
                break
        else:
            return [], None
    elif rows and (h := map_header(rows[0])):
        hmap, body = h, rows[1:]  # 续表重复表头
    for r in body:
        target = items[-1] if items else tail
        if target and is_continuation(r, hmap):
            target.description = f"{target.description} {norm(r[hmap['description']])}".strip()
            continue
        it = row_to_item(r, hmap, start_line + len(items), source)
        if it:
            items.append(it)
    return items, hmap
