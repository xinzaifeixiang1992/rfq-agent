"""解析器：Excel 合并单元格/分组行、PDF 跨页续表与断行续写、eml 附件与头部字段、物料匹配四种路径。"""
from pathlib import Path

import pytest

from app.parsers.dispatcher import parse_attachment
from app.parsers.email_parser import parse_eml, rule_header
from app.pricing.matcher import SkuMatcher
from app.schemas import LineItem
from app.services import build_services
from scripts import make_samples as ms


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    ms.make_excel(d / "a.xlsx")
    ms.make_pdf(d / "b.pdf")
    return d


def test_excel_merged_and_group_rows(samples, tmp_path):
    r = parse_attachment(str(samples / "a.xlsx"), tmp_path)
    assert [i.part_no for i in r.items][:3] == ["NA-RLY-40", "RLY-0136", ms.alias("RLY-0141")]
    assert len(r.items) == 6  # 分组行、Total 行被跳过
    assert all(i.unit == "pcs" for i in r.items[:3])  # 纵向合并单元格展开
    assert r.items[3].part_no is None and r.items[3].target_price == 0.03


def test_pdf_cross_page(samples, tmp_path):
    r = parse_attachment(str(samples / "b.pdf"), tmp_path)
    assert len(r.items) == 34
    assert r.items[24].part_no == "PE-CAB-15BK" and r.items[24].description.endswith("UL1015 105°C, RoHS")
    assert any("续表" in n for n in r.notes)


def test_eml_attachment_and_header(samples, tmp_path):
    ms.eml(tmp_path / "x.eml", "Erik <erik@nordic-auto.se>", "RFQ test", "Terms CIF Gothenburg, currency EUR.",
           [samples / "a.xlsx"])
    p = parse_eml((tmp_path / "x.eml").read_bytes(), tmp_path / "att")
    assert p.subject == "RFQ test" and len(p.attachments) == 1 and Path(p.attachments[0]).exists()
    h = rule_header(p.subject, p.body, p.sender)
    assert (h.incoterm, h.currency) == ("CIF", "EUR")


def test_matcher_paths(settings):
    from app.seed import seed, seed_memories
    s = build_services(settings)
    seed(s.db, history=50)
    seed_memories(s.memory)
    mems = s.memory.retrieve("NA-RLY-40", customer_id="C001", top_k=20, kinds=["alias"])
    m = SkuMatcher(s.db)

    def run(part, desc):
        return m.match(LineItem(line_no=1, part_no=part, description=desc, qty=1), mems)

    assert run("RLY-0136", "").match_method == "exact"
    assert run(ms.alias("RLY-0141"), "").sku == "RLY-0141"
    r = run("NA-RLY-40", "relay")
    assert (r.match_method, r.sku) == ("memory", "RLY-0131")
    r = run(None, "Nylon cable tie 4.8x200 black UV")
    assert (r.match_method, r.sku) == ("fuzzy", "TIE-0112")
    assert run("NA-SENSOR-77", "Hall sensor custom").match_method == "none"
