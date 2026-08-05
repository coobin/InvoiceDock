from types import SimpleNamespace

from app.services.verifier import (
    _provider_fields_valid,
    compare_sources,
    has_invoice_identity,
    map_external_fields,
)


def test_dual_source_marks_consistent_when_critical_fields_match():
    ocr = {
        "invoice_number": "12345678",
        "invoice_date": "2026-01-02",
        "total_amount": 100.0,
        "seller_name": "示例 公司",
        "tax_amount": 5.66,
        "category": "住宿",
    }
    llm = {
        "invoice_number": "12345678",
        "invoice_date": "2026/01/02",
        "total_amount": 100.00,
        "seller_name": "示例公司",
        "tax_amount": 5.66,
        "category": "住宿",
    }
    merged, conflicts, confidence, consistent = compare_sources(ocr, llm)
    assert consistent is True
    assert conflicts == []
    assert confidence == 1.0
    assert merged["invoice_number"] == "12345678"


def test_critical_conflict_requires_review():
    ocr = {"invoice_number": "11111111", "invoice_date": "2026-01-02", "total_amount": 100.0, "seller_name": "A"}
    llm = {"invoice_number": "22222222", "invoice_date": "2026-01-02", "total_amount": 100.0, "seller_name": "A"}
    _merged, conflicts, _confidence, consistent = compare_sources(ocr, llm)
    assert consistent is False
    assert conflicts[0]["field"] == "invoice_number"


def test_provider_fields_valid_requires_real_invoice_identity():
    real = {"invoice_number": "26117000001062157048", "total_amount": 898.0, "seller_name": "腾讯云计算（北京）有限责任公司"}
    assert _provider_fields_valid(real) is True
    assert _provider_fields_valid({**real, "total_amount": 0.0}) is True
    assert _provider_fields_valid({"invoice_number": "", "total_amount": 20.0, "seller_name": ""}) is False
    assert _provider_fields_valid({"invoice_number": "123", "total_amount": None, "seller_name": "A"}) is False
    assert _provider_fields_valid({"invoice_number": "123", "total_amount": 20.0, "seller_name": "", "buyer_name": ""}) is False
    assert _provider_fields_valid({"invoice_number": "123", "total_amount": 20.0, "buyer_name": "B"}) is True


def test_has_invoice_identity():
    assert has_invoice_identity(SimpleNamespace(invoice_number="1", total_amount=None, seller_name="", buyer_name="")) is True
    assert has_invoice_identity(SimpleNamespace(invoice_number="", total_amount=0.0, seller_name="", buyer_name="")) is True
    assert has_invoice_identity(SimpleNamespace(invoice_number="", total_amount=None, seller_name="A", buyer_name="")) is True
    assert has_invoice_identity(SimpleNamespace(invoice_number="", total_amount=None, seller_name="", buyer_name="")) is False


def test_kingdee_alias_mapping():
    fields = map_external_fields(
        {"invoiceNo": "24500000123456789012", "salerName": "销售方", "buyerName": "购买方", "totalAmount": "99.80"}
    )
    assert fields["invoice_number"] == "24500000123456789012"
    assert fields["seller_name"] == "销售方"
    assert fields["total_amount"] == 99.8
