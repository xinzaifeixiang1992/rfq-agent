"""附件分发：按扩展名路由到对应解析器。MOI 多模态解析可在此作为优先解析器接入。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.parsers.excel_parser import parse_csv, parse_excel
from app.parsers.pdf_parser import parse_pdf
from app.parsers.text_layer import excel_text, pdf_text
from app.schemas import LineItem


@dataclass
class AttachmentResult:
    file: str
    items: list[LineItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # 需视觉模型识别的页面/图片
    complexity: str = "normal"
    text: str = ""  # 可见全文（供 MemoryGate 审查）
    hidden: str = ""  # 隐藏文字（白字/极小字号/隐藏行列）


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def parse_attachment(path: str, work_dir: Path) -> AttachmentResult:
    p = Path(path)
    ext = p.suffix.lower()
    res = AttachmentResult(file=p.name)
    try:
        if ext in (".xlsx", ".xlsm"):
            res.items, res.notes = parse_excel(p)
            res.text, res.hidden = excel_text(p)
        elif ext == ".csv":
            res.items, res.notes = parse_csv(p)
        elif ext == ".pdf":
            res.items, res.notes, res.images, res.complexity = parse_pdf(p, work_dir)
            res.text, res.hidden = pdf_text(p)
        elif ext in IMAGE_EXT:
            res.images, res.complexity = [str(p)], "high"
            res.notes.append("图片附件，转视觉模型")
        else:
            res.notes.append(f"不支持的附件类型: {ext}")
    except Exception as e:  # noqa: BLE001 - 单个附件失败不影响整单
        res.notes.append(f"解析失败: {e}")
    if res.hidden:
        res.notes.append(f"发现隐藏文字 {len(res.hidden)} 字符，已交 MemoryGate 审查")
    return res
