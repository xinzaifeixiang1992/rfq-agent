"""邮件解析与规则抽取（规则引擎同时作为模型路由的最终兜底）。"""
from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from pathlib import Path

from app.schemas import LineItem, RFQHeader

CURRENCIES = ("USD", "EUR", "CNY", "RMB", "JPY", "GBP", "HKD")
INCOTERMS = ("EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP")


@dataclass
class ParsedEmail:
    subject: str = ""
    sender: str = ""
    body: str = ""
    attachments: list[str] = field(default_factory=list)


def parse_eml(raw: bytes, attach_dir: Path) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=policy.default)
    out = ParsedEmail(subject=str(msg.get("Subject", "")), sender=str(msg.get("From", "")))
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        text = body.get_content()
        out.body = re.sub(r"<[^>]+>", " ", text) if body.get_content_subtype() == "html" else text
    attach_dir.mkdir(parents=True, exist_ok=True)
    for part in msg.iter_attachments():
        name = Path(part.get_filename() or "attachment.bin").name
        p = attach_dir / name
        p.write_bytes(part.get_payload(decode=True) or b"")
        out.attachments.append(str(p))
    return out


def sender_domain(sender: str) -> str:
    m = re.search(r"@([\w\.\-]+)", sender or "")
    return m.group(1).lower() if m else ""


def rule_header(subject: str, body: str, sender: str) -> RFQHeader:
    text = f"{subject}\n{body}"
    up = text.upper()
    low = text.lower()
    intent = "new_rfq"
    if re.search(r"\b(revis|updated?|change)\w*", low):
        intent = "revision"
    elif re.search(r"follow[\s-]?up|reminder|催", low):
        intent = "follow_up"
    cur = next((c for c in CURRENCIES if re.search(rf"\b{c}\b", up)), None)
    inc = next((i for i in INCOTERMS if re.search(rf"\b{i}\b", up)), None)
    dest = None
    if m := re.search(rf"\b(?:{'|'.join(INCOTERMS)})\s+([A-Z][A-Za-z]+(?:\s[A-Z][a-z]+)?)", text):
        dest = m.group(1)
    elif m := re.search(r"destination[:\s]+([A-Za-z ]{3,30})", text, re.I):
        dest = m.group(1).strip()
    deadline = None
    if m := re.search(r"(?:delivery|lead time|交期)[^\n\.]{0,40}?(\d+\s*(?:weeks?|days?|周|天)|\d{4}-\d{2}-\d{2})", text, re.I):
        deadline = m.group(1)
    pay = None
    if m := re.search(r"\b(T/T|L/C|D/P|net\s*\d+)\b", text, re.I):
        pay = m.group(1).upper()
    name = None
    if m := re.search(r"(?:regards|best|sincerely)[,\s]*\n+\s*[^\n]+\n\s*([^\n]+)", text, re.I):
        name = m.group(1).strip()
    if cur == "RMB":
        cur = "CNY"
    h = RFQHeader(intent=intent, customer_name=name, sender_email=sender, currency=cur, incoterm=inc,
                  destination=dest, delivery_deadline=deadline, payment_terms=pay)
    h.missing_fields = [f for f in ("currency", "incoterm", "destination", "delivery_deadline")
                        if not getattr(h, f)]
    h.confidence = round(1 - 0.12 * len(h.missing_fields), 2)
    return h


_LINE = re.compile(
    r"(?P<part>[A-Z0-9][A-Z0-9\-\./]{3,})\s*[,:\-–]?\s*(?P<desc>[^\n\d]{0,60}?)[\s,:\-–x×]*"
    r"(?P<qty>\d[\d,]*)\s*(?P<unit>pcs|pc|units?|sets?|m|meters?|kg|rolls?|件|个|套|米)\b", re.I)


def rule_body_items(body: str) -> list[LineItem]:
    items = []
    for m in _LINE.finditer(body):
        items.append(LineItem(line_no=len(items) + 1, part_no=m.group("part").strip(),
                              description=m.group("desc").strip(" ,-:"),
                              qty=float(m.group("qty").replace(",", "")), unit=m.group("unit").lower(),
                              source="email-body", confidence=0.75))
    return items
