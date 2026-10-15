"""报价单输出：Excel / PDF / HTML 与回复邮件规则模板。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from app.prompts import ReplyDraft

COLS = [("line_no", "No."), ("sku", "SKU"), ("description", "Description"), ("qty", "Qty"),
        ("unit", "Unit"), ("unit_price", "Unit Price"), ("amount", "Amount")]


def _rows(lines: list[dict]) -> list[dict]:
    out = []
    for ln in sorted(lines, key=lambda x: x["line_no"]):
        out.append({**ln, "amount": round(float(ln["unit_price"]) * float(ln["qty"]), 2)})
    return out


def _meta_pairs(rfq_id: str, header: dict, customer: dict | None) -> list[tuple[str, str]]:
    return [("Quotation No.", rfq_id), ("Date", date.today().isoformat()),
            ("Customer", (customer or {}).get("name") or header.get("customer_name") or "-"),
            ("Currency", header.get("currency") or "-"), ("Incoterm", header.get("incoterm") or "-"),
            ("Destination", header.get("destination") or "-"),
            ("Delivery", header.get("delivery_deadline") or "-"), ("Payment", header.get("payment_terms") or "-"),
            ("Validity", "30 days")]


def render_excel(path: Path, rfq_id: str, header: dict, customer: dict | None, lines: list[dict]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Quotation"
    ws["A1"] = "QUOTATION"
    ws["A1"].font = Font(size=16, bold=True)
    r = 3
    for k, v in _meta_pairs(rfq_id, header, customer):
        ws.cell(r, 1, k).font = Font(bold=True)
        ws.cell(r, 2, v)
        r += 1
    r += 1
    fill = PatternFill("solid", fgColor="DDEBF7")
    for j, (_, title) in enumerate(COLS, 1):
        c = ws.cell(r, j, title)
        c.font, c.fill, c.alignment = Font(bold=True), fill, Alignment(horizontal="center")
    rows = _rows(lines)
    for ln in rows:
        r += 1
        for j, (k, _) in enumerate(COLS, 1):
            ws.cell(r, j, ln.get(k))
    r += 1
    ws.cell(r, 6, "Total").font = Font(bold=True)
    ws.cell(r, 7, round(sum(x["amount"] for x in rows), 2)).font = Font(bold=True)
    for col, w in zip("ABCDEFG", (14, 16, 48, 10, 8, 14, 16)):
        ws.column_dimensions[col].width = w
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def render_pdf(path: Path, rfq_id: str, header: dict, customer: dict | None, lines: list[dict]) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))  # 内置 CID 字体，支持中文且无需外部字体文件
    st = getSampleStyleSheet()
    for s in st.byName.values():
        s.fontName = "STSong-Light"
    rows = _rows(lines)
    meta = Table([[k, v] for k, v in _meta_pairs(rfq_id, header, customer)], colWidths=[90, 380])
    meta.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "STSong-Light"), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    data = [[t for _, t in COLS]]
    for ln in rows:
        data.append([str(ln["line_no"]), ln.get("sku") or "", Paragraph(escape(str(ln.get("description") or "")), st["BodyText"]),
                     f"{ln['qty']:g}", ln.get("unit") or "", f"{ln['unit_price']:.4f}", f"{ln['amount']:,.2f}"])
    data.append(["", "", "", "", "", "Total", f"{sum(x['amount'] for x in rows):,.2f}"])
    tbl = Table(data, colWidths=[30, 70, 190, 45, 35, 60, 70], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDEBF7")),
        ("GRID", (0, 0), (-1, -2), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(path), pagesize=A4).build(
        [Paragraph("QUOTATION", st["Title"]), meta, Spacer(1, 12), tbl])
    return path


_HTML = Environment(autoescape=True).from_string("""<!doctype html><html><head><meta charset="utf-8"><title>{{ rfq_id }}</title>
<style>body{font-family:Arial,"Microsoft YaHei";margin:32px}table{border-collapse:collapse;width:100%}
td,th{border:1px solid #ccc;padding:6px;font-size:13px}th{background:#DDEBF7}.meta td{border:none;padding:2px 8px}</style>
</head><body><h2>QUOTATION</h2><table class="meta">{% for k, v in meta %}<tr><td><b>{{ k }}</b></td><td>{{ v }}</td></tr>{% endfor %}</table>
<br><table><tr>{% for _, t in cols %}<th>{{ t }}</th>{% endfor %}</tr>
{% for ln in rows %}<tr>{% for k, _ in cols %}<td>{{ ln[k] if ln[k] is not none else "" }}</td>{% endfor %}</tr>{% endfor %}
<tr><td colspan="6" align="right"><b>Total</b></td><td><b>{{ "{:,.2f}".format(total) }}</b></td></tr></table></body></html>""")


def render_html(path: Path, rfq_id: str, header: dict, customer: dict | None, lines: list[dict]) -> Path:
    rows = _rows(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HTML.render(rfq_id=rfq_id, meta=_meta_pairs(rfq_id, header, customer), cols=COLS, rows=rows,
                                 total=sum(x["amount"] for x in rows)), encoding="utf-8")
    return path


def reply_rule(rfq_id: str, header: dict, customer: dict | None, lines: list[dict]) -> ReplyDraft:
    rows = _rows(lines)
    cur = header.get("currency") or ""
    body = [f"Dear {(customer or {}).get('name') or header.get('customer_name') or 'Customer'},", "",
            "Thank you for your inquiry. Please find our quotation below and in the attachment.", ""]
    body += [f"- {x['line_no']}. {x.get('sku') or ''} {x.get('description') or ''}: {x['qty']:g} {x.get('unit') or ''}"
             f" @ {cur} {x['unit_price']:.4f}" for x in rows]
    body += ["", f"Total: {cur} {sum(x['amount'] for x in rows):,.2f}",
             f"Terms: {header.get('incoterm') or 'TBD'} {header.get('destination') or ''}; "
             f"Payment: {header.get('payment_terms') or 'TBD'}; Validity: 30 days.", "", "Best regards,", "Sales Team"]
    return ReplyDraft(subject=f"Quotation {rfq_id}", body="\n".join(body), confidence=0.9)


def clarification_rule(header: dict, missing: list[str]) -> str:
    names = {"currency": "quotation currency", "incoterm": "Incoterm (e.g. FOB / CIF / DDP)",
             "destination": "destination port / city", "delivery_deadline": "required delivery date"}
    ask = "\n".join(f"- {names.get(f, f)}" for f in missing)
    return (f"Dear {header.get('customer_name') or 'Customer'},\n\nThank you for your inquiry. To provide an accurate "
            f"quotation, could you please confirm:\n{ask}\n\nWe have prepared a preliminary quotation based on default "
            f"terms in the meantime.\n\nBest regards,\nSales Team")
