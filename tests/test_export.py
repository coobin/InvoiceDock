from datetime import datetime
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader

from app.models import Invoice
from app.services import export_service


class TemporarySettings:
    def __init__(self, root: Path):
        self.upload_dir = root
        self.preview_dir = root / "previews"


def test_print_pdf_places_two_invoices_on_one_a4_page(tmp_path, monkeypatch):
    monkeypatch.setattr(export_service, "get_settings", lambda: TemporarySettings(tmp_path))
    image_path = tmp_path / "sample.jpg"
    Image.new("RGB", (1000, 600), "white").save(image_path)
    invoices = [
        Invoice(original_name=f"sample-{index}.jpg", stored_name="sample.jpg", mime_type="image/jpeg", file_size=1, sha256=str(index))
        for index in range(2)
    ]
    output = export_service.make_print_pdf(invoices, per_page=2)
    reader = PdfReader(__import__("io").BytesIO(output))
    assert len(reader.pages) == 1
    assert round(float(reader.pages[0].mediabox.width)) == 595
    assert round(float(reader.pages[0].mediabox.height)) == 842


def test_workbook_contains_invoice_ledger_row():
    invoice = Invoice(
        original_name="invoice.pdf",
        stored_name="invoice.pdf",
        mime_type="application/pdf",
        file_size=1,
        sha256="abc",
        status="verified",
        verification_method="kingdee",
        invoice_number="24500000000012345678",
        seller_name="远山示例酒店有限公司",
        total_amount=500.0,
        created_at=datetime(2026, 8, 5, 12, 0, 0),
    )
    workbook = load_workbook(BytesIO(export_service.make_invoice_workbook([invoice])))
    sheet = workbook["发票台账"]
    assert sheet.freeze_panes == "A2"
    assert sheet["A2"].value == "verified"
    assert sheet["E2"].value == "24500000000012345678"
    assert sheet["G2"].value == "远山示例酒店有限公司"
    assert sheet["M2"].value == 500.0
