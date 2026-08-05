from app.services.verifier import compare_sources, map_external_fields


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


def test_kingdee_alias_mapping():
    fields = map_external_fields(
        {"invoiceNo": "24500000123456789012", "salerName": "销售方", "buyerName": "购买方", "totalAmount": "99.80"}
    )
    assert fields["invoice_number"] == "24500000123456789012"
    assert fields["seller_name"] == "销售方"
    assert fields["total_amount"] == 99.8

