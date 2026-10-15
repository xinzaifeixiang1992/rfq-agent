"""附件全文与隐藏文字抽取，供 MemoryGate 审查（表格解析只关心物料行，这里关心"表格以外说了什么"）。

隐藏文字：PDF 中白色填充或字号 < 3pt 的字符；Excel 中白色字体、隐藏行/列/工作表中的文本。
"""
from __future__ import annotations

from pathlib import Path

_WHITE = {(1.0,), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0, 0.0)}  # 灰度 / RGB / CMYK 的白色


def _pdf_hidden(obj: dict) -> bool:
    if obj.get("object_type") != "char":
        return False
    c = obj.get("non_stroking_color")
    if c is not None:
        c = tuple(round(float(x), 2) for x in (c if isinstance(c, (list, tuple)) else [c]))
        if c in _WHITE:
            return True
    return float(obj.get("size") or 10) < 3


def pdf_text(path: Path) -> tuple[str, str]:
    import pdfplumber

    vis, hid = [], []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            vis.append(page.filter(lambda o: not _pdf_hidden(o)).extract_text() or "")
            hid.append(page.filter(_pdf_hidden).extract_text() or "")
    return "\n".join(vis).strip(), "\n\n".join(h for h in hid if h.strip()).strip()


def excel_text(path: Path) -> tuple[str, str]:
    from openpyxl import load_workbook

    vis, hid = [], []
    wb = load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        sheet_hidden = ws.sheet_state != "visible"
        for row in ws.iter_rows():
            for cell in row:
                if not isinstance(cell.value, str) or not cell.value.strip():
                    continue
                color = getattr(getattr(cell.font, "color", None), "rgb", None)
                hidden = (sheet_hidden or ws.row_dimensions[cell.row].hidden
                          or ws.column_dimensions[cell.column_letter].hidden
                          or (isinstance(color, str) and color.upper().endswith("FFFFFF")))
                (hid if hidden else vis).append(cell.value.strip())
    return "\n".join(vis), "\n\n".join(hid)
