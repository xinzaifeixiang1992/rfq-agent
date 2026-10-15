"""Excel/CSV 解析：展开合并单元格、多 Sheet、自动定位表头。"""
from __future__ import annotations

import csv
from pathlib import Path

from app.parsers.table_utils import table_to_items
from app.schemas import LineItem


def _sheet_grid(ws) -> list[list]:
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    for rng in ws.merged_cells.ranges:
        v = ws.cell(rng.min_row, rng.min_col).value
        for r in range(rng.min_row - 1, min(rng.max_row, len(grid))):
            for c in range(rng.min_col - 1, rng.max_col):
                if c < len(grid[r]):
                    grid[r][c] = v
    return grid


def parse_excel(path: Path) -> tuple[list[LineItem], list[str]]:
    from openpyxl import load_workbook

    items: list[LineItem] = []
    notes: list[str] = []
    wb = load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        grid = _sheet_grid(ws)
        if ws.merged_cells.ranges:
            notes.append(f"{ws.title}: 展开 {len(ws.merged_cells.ranges)} 个合并单元格")
        got, hmap = table_to_items(grid, f"{path.name}#{ws.title}", start_line=len(items) + 1)
        if hmap is None:
            notes.append(f"{ws.title}: 未识别到表头，跳过")
        items.extend(got)
    return items, notes


def parse_csv(path: Path) -> tuple[list[LineItem], list[str]]:
    for enc in ("utf-8-sig", "gbk"):
        try:
            rows = list(csv.reader(path.read_text(encoding=enc).splitlines()))
            break
        except UnicodeDecodeError:
            continue
    else:
        return [], ["CSV 编码无法识别"]
    items, hmap = table_to_items(rows, path.name)
    return items, [] if hmap else ["CSV 未识别到表头"]
