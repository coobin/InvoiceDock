from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import NameObject, RectangleObject

from app.config import get_settings
from app.models import ExpenseItem, Invoice, Project
from app.services.extractor import render_first_page

A4_WIDTH = 595.28
A4_HEIGHT = 841.89
EXCEL_FORMULA_PREFIXES = ("=", "+", "-", "@")
EXCEL_ILLEGAL_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _excel_safe(value: object) -> object:
    """Return user-controlled text without allowing spreadsheet formulas.

    Invoice fields originate in uploaded files and email attachment names.  A
    leading formula marker would otherwise be evaluated when an administrator
    opens the exported workbook, which can trigger links or external actions.
    Numeric and date values are preserved as their native types.
    """
    if not isinstance(value, str):
        return value
    cleaned = EXCEL_ILLEGAL_CONTROL_CHARS.sub(" ", value)
    if cleaned.lstrip().startswith(EXCEL_FORMULA_PREFIXES):
        return "'" + cleaned
    return cleaned


def _reader_for_invoice(invoice: Invoice) -> PdfReader:
    path = get_settings().upload_dir / invoice.stored_name
    if invoice.mime_type == "application/pdf":
        return PdfReader(str(path))
    if invoice.mime_type.startswith("image/"):
        image = Image.open(path).convert("RGB")
        buffer = BytesIO()
        image.save(buffer, "PDF", resolution=150.0)
        buffer.seek(0)
        return PdfReader(buffer)
    raise ValueError(f"{invoice.original_name} 不是可排版的 PDF 或图片")


def _place_page(canvas: PageObject, source: PageObject, x: float, y: float, width: float, height: float) -> None:
    source_width = float(source.mediabox.width)
    source_height = float(source.mediabox.height)
    if source_width <= 0 or source_height <= 0:
        return
    scale = min(width / source_width, height / source_height)
    target_width = source_width * scale
    target_height = source_height * scale
    tx = x + (width - target_width) / 2
    ty = y + (height - target_height) / 2
    transform = Transformation().scale(scale, scale).translate(tx, ty)
    existing_annotation_count = len(canvas.get("/Annots", []))
    canvas.merge_transformed_page(source, transform, over=True)
    annotations = canvas.get("/Annots", [])
    for annotation_reference in annotations[existing_annotation_count:]:
        annotation = annotation_reference.get_object()
        rectangle = annotation.get("/Rect")
        if rectangle is None or len(rectangle) != 4:
            continue
        first = transform.apply_on((rectangle[0], rectangle[1]))
        second = transform.apply_on((rectangle[2], rectangle[3]))
        annotation[NameObject("/Rect")] = RectangleObject(
            (
                min(first[0], second[0]),
                min(first[1], second[1]),
                max(first[0], second[0]),
                max(first[1], second[1]),
            )
        )


def make_print_pdf(invoices: list[Invoice], per_page: int = 2) -> bytes:
    if per_page not in {1, 2, 4}:
        raise ValueError("每页数量只支持 1、2 或 4")
    if not invoices:
        raise ValueError("至少选择一张发票")
    readers = [(invoice, _reader_for_invoice(invoice)) for invoice in invoices]
    writer = PdfWriter()
    page_width, page_height = (A4_HEIGHT, A4_WIDTH) if per_page == 4 else (A4_WIDTH, A4_HEIGHT)
    if per_page == 1:
        for _invoice, reader in readers:
            for source in reader.pages:
                canvas = PageObject.create_blank_page(width=page_width, height=page_height)
                _place_page(canvas, source, 28, 28, page_width - 56, page_height - 56)
                writer.add_page(canvas)
    else:
        margin = 24.0
        gap = 12.0
        columns = 1 if per_page == 2 else 2
        rows = 2
        slot_width = (page_width - margin * 2 - gap * (columns - 1)) / columns
        slot_height = (page_height - margin * 2 - gap * (rows - 1)) / rows
        for batch_start in range(0, len(readers), per_page):
            canvas = PageObject.create_blank_page(width=page_width, height=page_height)
            for index, (_invoice, reader) in enumerate(readers[batch_start : batch_start + per_page]):
                row = index // columns
                col = index % columns
                x = margin + col * (slot_width + gap)
                y = page_height - margin - (row + 1) * slot_height - row * gap
                _place_page(canvas, reader.pages[0], x, y, slot_width, slot_height)
            writer.add_page(canvas)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def make_invoice_workbook(
    invoices: list[Invoice],
    projects_map: dict[str, Project] | None = None,
    expenses: list[ExpenseItem] | None = None,
    invoices_map: dict[str, Invoice] | None = None,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "发票台账"
    headers = [
        "状态", "查验方式", "发票类型", "发票代码", "发票号码", "开票日期", "销售方", "销售方税号",
        "购买方", "购买方税号", "不含税金额", "税额", "价税合计", "分类", "来源", "抬头警示", "原文件名", "入库时间",
        "所属项目", "项目编号",
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="173B5E")
        cell.alignment = Alignment(horizontal="center")
    proj_map = projects_map or {}
    for item in invoices:
        proj = proj_map.get(item.project_id) if item.project_id else None
        row = [
            item.status,
            item.verification_method,
            item.invoice_type,
            item.invoice_code,
            item.invoice_number,
            item.invoice_date,
            item.seller_name,
            item.seller_tax_id,
            item.buyer_name,
            item.buyer_tax_id,
            item.amount,
            item.tax_amount,
            item.total_amount,
            item.category,
            item.source,
            item.title_warning,
            item.original_name,
            item.created_at.isoformat(sep=" ", timespec="seconds"),
            proj.name if proj else "",
            proj.code if proj else "",
        ]
        sheet.append([_excel_safe(value) for value in row])
    widths = [12, 12, 20, 16, 22, 14, 32, 22, 32, 22, 15, 15, 15, 14, 14, 24, 36, 21, 20, 16]
    for index, width in enumerate(widths, start=1):
        col_letter = chr(64 + index) if index <= 26 else f"A{chr(64 + index - 26)}"
        sheet.column_dimensions[col_letter].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    if expenses is not None:
        sheet2 = workbook.create_sheet(title="待开票预录")
        exp_headers = [
            "状态", "所属项目", "报销人", "发生日期", "预计金额", "费用分类", "预计开票方", "预计开票日", "核销发票号", "备注", "创建时间",
        ]
        sheet2.append(exp_headers)
        for cell in sheet2[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2D5A27")
            cell.alignment = Alignment(horizontal="center")
        inv_map = invoices_map or {}
        status_names = {"pending": "待开票", "reconciled": "已核销", "cancelled": "已作废"}
        for exp in expenses:
            proj = proj_map.get(exp.project_id) if exp.project_id else None
            rec_inv = inv_map.get(exp.reconciled_invoice_id) if exp.reconciled_invoice_id else None
            rec_str = (
                f"{rec_inv.invoice_number or rec_inv.original_name} (¥{rec_inv.total_amount:.2f})"
                if rec_inv and rec_inv.total_amount is not None
                else (rec_inv.invoice_number or rec_inv.original_name if rec_inv else "")
            )
            exp_row = [
                status_names.get(exp.status, exp.status),
                proj.name if proj else "",
                exp.claimant,
                exp.expense_date,
                exp.expected_amount,
                exp.category,
                exp.expected_seller,
                exp.expected_invoice_date,
                rec_str,
                exp.notes,
                exp.created_at.isoformat(sep=" ", timespec="seconds"),
            ]
            sheet2.append([_excel_safe(value) for value in exp_row])
        exp_widths = [12, 18, 14, 14, 15, 14, 28, 14, 28, 28, 21]
        for index, width in enumerate(exp_widths, start=1):
            col_letter = chr(64 + index) if index <= 26 else f"A{chr(64 + index - 26)}"
            sheet2.column_dimensions[col_letter].width = width
        sheet2.freeze_panes = "A2"
        sheet2.auto_filter.ref = sheet2.dimensions

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def make_preview(invoice: Invoice) -> Path | None:
    settings = get_settings()
    target = settings.preview_dir / f"{invoice.id}.jpg"
    if target.exists():
        return target
    source = settings.upload_dir / invoice.stored_name
    try:
        if invoice.mime_type == "application/pdf":
            image = render_first_page(source, scale=1.4)
        elif invoice.mime_type.startswith("image/"):
            image = Image.open(source).convert("RGB")
        else:
            return None
        image.thumbnail((1200, 1200))
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format="JPEG", quality=82, optimize=True)
        return target
    except Exception:
        return None
