"""PDF 解析：逐页抽表并做跨页续表合并；无文本层（扫描件）时导出页面图片交给视觉模型。"""
from __future__ import annotations

from pathlib import Path

from app.parsers.table_utils import table_to_items
from app.schemas import LineItem


def parse_pdf(path: Path, image_dir: Path) -> tuple[list[LineItem], list[str], list[str], str]:
    """返回 (条目, 处理说明, 待视觉识别的页面图片, 复杂度)。"""
    import pdfplumber

    items: list[LineItem] = []
    notes: list[str] = []
    images: list[str] = []
    hmap = None
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if not text:
                img = image_dir / f"{path.stem}_p{pno}.png"
                img.parent.mkdir(parents=True, exist_ok=True)
                page.to_image(resolution=150).save(img)
                images.append(str(img))
                notes.append(f"第 {pno} 页无文本层，转视觉模型")
                continue
            for t in page.extract_tables():
                cont = hmap is not None
                got, hmap_new = table_to_items(t, f"{path.name}#p{pno}", hmap=hmap, start_line=len(items) + 1,
                                               tail=items[-1] if items else None)
                if hmap_new is None:  # 既无可识别表头，也不是续表
                    continue
                if cont and pno > 1:
                    notes.append(f"第 {pno} 页按续表合并 {len(got)} 行")
                hmap = hmap_new
                items.extend(got)
    complexity = "high" if images or n_pages > 3 else "normal"
    return items, notes, images, complexity
