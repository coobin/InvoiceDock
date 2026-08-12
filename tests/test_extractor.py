import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest

from app.services import extractor
from app.services.extractor import (
    categorize,
    extract_ofd_text,
    extract_xml_text,
    parse_invoice_fields,
)


def test_parse_common_vat_invoice_fields():
    text = """
    增值税电子普通发票
    发票代码：031002400111 发票号码：12345678
    开票日期：2026年07月18日 校验码：12345678901234567890
    购买方信息 名称：示例科技有限公司
    纳税人识别号：91310000123456789A
    销售方信息 名称：示例酒店有限公司
    纳税人识别号：91310000987654321B
    金额合计 ¥943.40 税额合计 ¥56.60 价税合计（小写）¥1000.00
    住宿服务
    """
    result = parse_invoice_fields(text)
    assert result["invoice_code"] == "031002400111"
    assert result["invoice_number"] == "12345678"
    assert result["invoice_date"] == "2026-07-18"
    assert result["total_amount"] == 1000.00
    assert result["tax_amount"] == 56.60
    assert result["buyer_tax_id"] == "91310000123456789A"
    assert result["seller_tax_id"] == "91310000987654321B"
    assert result["category"] == "住宿"


def test_extract_structured_xml_and_map_fields():
    data = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Invoice><InvoiceNo>24500000000012345678</InvoiceNo><IssueDate>2026-08-01</IssueDate>
    <SellerName>Example Seller</SellerName><TotalAmount>88.50</TotalAmount></Invoice>"""
    text, structured = extract_xml_text(data)
    result = parse_invoice_fields(text, structured)
    assert result["invoice_number"] == "24500000000012345678"
    assert result["invoice_date"] == "2026-08-01"
    assert result["seller_name"] == "Example Seller"
    assert result["total_amount"] == 88.50


def test_categorize_expanded_keywords():
    assert categorize("标签机及耗材 430.49", "某某公司") == "办公用品"
    assert categorize("会议门牌充电器 82.4", "某某公司") == "办公用品"
    assert categorize("会议门牌变压 54.45", "某某公司") == "办公用品"
    assert categorize("打印机墨盒", "某某公司") == "办公用品"
    assert categorize("住宿服务", "示例酒店有限公司") == "住宿"
    assert categorize("滴滴出行", "滴滴出行科技有限公司") == "交通出行"
    assert categorize("快递费", "顺丰速运有限公司") == "物流"
    assert categorize("话费充值", "中国移动通信") == "通讯"
    assert categorize("会议餐饮费", "某会议中心") == "餐饮"
    assert categorize("某服务费", "无关公司") == "未分类"


def _write_ofd(path, files: dict[str, bytes]) -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    path.write_bytes(buffer.getvalue())


def test_extract_ofd_rejects_excessive_file_count(tmp_path, monkeypatch):
    path = tmp_path / "many.ofd"
    _write_ofd(path, {f"Doc_{index}.xml": b"<a>ok</a>" for index in range(3)})
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(max_ofd_files=2, max_ofd_uncompressed_mb=1),
    )

    with pytest.raises(ValueError, match="文件数量"):
        extract_ofd_text(path)


def test_extract_ofd_rejects_excessive_uncompressed_size(tmp_path, monkeypatch):
    path = tmp_path / "large.ofd"
    _write_ofd(path, {"Doc_0.xml": b"<a>" + b"x" * (1024 * 1024) + b"</a>"})
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(max_ofd_files=10, max_ofd_uncompressed_mb=1),
    )

    with pytest.raises(ValueError, match="解压后大小"):
        extract_ofd_text(path)
