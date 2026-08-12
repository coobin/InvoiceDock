from datetime import datetime
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Text

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


def test_print_pdf_places_four_invoices_on_landscape_a4(tmp_path, monkeypatch):
    monkeypatch.setattr(export_service, "get_settings", lambda: TemporarySettings(tmp_path))
    image_path = tmp_path / "sample.jpg"
    Image.new("RGB", (1000, 600), "white").save(image_path)
    invoices = [
        Invoice(original_name=f"sample-{index}.jpg", stored_name="sample.jpg", mime_type="image/jpeg", file_size=1, sha256=str(index))
        for index in range(4)
    ]
    output = export_service.make_print_pdf(invoices, per_page=4)
    page = PdfReader(BytesIO(output)).pages[0]
    assert round(float(page.mediabox.width)) == 842
    assert round(float(page.mediabox.height)) == 595


def test_print_pdf_transforms_stamp_annotation_with_invoice(tmp_path, monkeypatch):
    monkeypatch.setattr(export_service, "get_settings", lambda: TemporarySettings(tmp_path))
    source_path = tmp_path / "annotated.pdf"
    writer = PdfWriter()
    source_page = writer.add_blank_page(width=600, height=400)
    writer.add_annotation(source_page, Text(rect=(250, 320, 350, 380), text="stamp"))
    with source_path.open("wb") as output:
        writer.write(output)
    invoices = [
        Invoice(original_name=f"sample-{index}.pdf", stored_name="annotated.pdf", mime_type="application/pdf", file_size=1, sha256=str(index))
        for index in range(4)
    ]

    output = export_service.make_print_pdf(invoices, per_page=4)
    page = PdfReader(BytesIO(output)).pages[0]
    rectangles = [annotation.get_object()["/Rect"] for annotation in page["/Annots"]]
    centers = [
        ((float(rect[0]) + float(rect[2])) / 2, (float(rect[1]) + float(rect[3])) / 2)
        for rect in rectangles
    ]

    assert len(rectangles) == 4
    assert all(float(rect[2]) - float(rect[0]) < 70 for rect in rectangles)
    assert sum(x < export_service.A4_HEIGHT / 2 for x, _y in centers) == 2
    assert sum(y < export_service.A4_WIDTH / 2 for _x, y in centers) == 2


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
