from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytesseract
from defusedxml import ElementTree as SafeET
from PIL import Image
from pypdf import PdfReader

FIELDS = [
    "invoice_type",
    "invoice_code",
    "invoice_number",
    "invoice_date",
    "check_code",
    "seller_name",
    "seller_tax_id",
    "buyer_name",
    "buyer_tax_id",
    "amount",
    "tax_amount",
    "total_amount",
    "category",
]


def _clean_text(value: str) -> str:
    value = value.replace("\x00", " ").replace("\u3000", " ")
    return re.sub(r"[ \t]+", " ", value)


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:8])


def render_first_page(path: Path, scale: float = 2.2) -> Image.Image:
    document = pdfium.PdfDocument(str(path))
    try:
        page = document[0]
        try:
            bitmap = page.render(scale=scale)
            try:
                return bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def extract_xml_text(data: bytes) -> tuple[str, dict[str, str]]:
    root = SafeET.fromstring(data)
    pairs: dict[str, str] = {}
    text_parts: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        value = (element.text or "").strip()
        if value:
            pairs[tag] = value
            text_parts.append(f"{tag}: {value}")
    return "\n".join(text_parts), pairs


def extract_ofd_text(path: Path) -> tuple[str, dict[str, str]]:
    pairs: dict[str, str] = {}
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")][:100]
        for name in xml_names:
            try:
                text, current = extract_xml_text(archive.read(name))
            except Exception:
                continue
            pairs.update(current)
            if text:
                parts.append(text)
    return "\n".join(parts), pairs


def extract_document(path: Path, mime_type: str) -> tuple[str, dict[str, str], Image.Image | None]:
    structured: dict[str, str] = {}
    preview: Image.Image | None = None
    if mime_type == "application/pdf":
        text = extract_pdf_text(path)
        preview = render_first_page(path)
        if len(re.sub(r"\s", "", text)) < 80:
            text = pytesseract.image_to_string(preview, lang="chi_sim+eng")
    elif mime_type.startswith("image/"):
        preview = Image.open(path).convert("RGB")
        text = pytesseract.image_to_string(preview, lang="chi_sim+eng")
    elif mime_type in {"application/xml", "text/xml"}:
        text, structured = extract_xml_text(path.read_bytes())
    elif mime_type == "application/ofd":
        text, structured = extract_ofd_text(path)
    else:
        raise ValueError(f"暂不支持识别格式：{mime_type}")
    return _clean_text(text), structured, preview


def _first_match(patterns: list[str], text: str, flags: int = 0) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip(" ：:，,|_-")
    return ""


def _number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return round(float(value.replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def _structured_value(values: dict[str, str], keys: set[str]) -> str:
    lowered = {key.lower(): value for key, value in values.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return ""


def parse_invoice_fields(text: str, structured: dict[str, str] | None = None) -> dict[str, Any]:
    structured = structured or {}
    compact = text.replace(" ", "")
    invoice_number = _structured_value(structured, {"InvoiceNo", "InvoiceNumber", "Fphm", "Number"}) or _first_match(
        [r"(?:发票号码|票据号码|Invoice\s*No\.?)[：:\s]*([0-9A-Za-z]{8,24})", r"\b([0-9]{20})\b"], text, re.I
    )
    invoice_code = _structured_value(structured, {"InvoiceCode", "Fpdm", "Code"}) or _first_match(
        [r"(?:发票代码|Invoice\s*Code)[：:\s]*([0-9]{10,12})"], text, re.I
    )
    invoice_date = _structured_value(structured, {"IssueDate", "InvoiceDate", "Kprq", "Date"}) or _first_match(
        [r"(?:开票日期|填开日期|出票日期|Issue\s*Date)[：:\s]*(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}日?)"], text, re.I
    )
    invoice_date = re.sub(r"[年/.]", "-", invoice_date).replace("月", "-").replace("日", "")
    check_code = _structured_value(structured, {"CheckCode", "Jym"}) or _first_match(
        [r"(?:校验码|Check\s*Code)[：:\s]*([0-9 ]{6,24})"], text, re.I
    ).replace(" ", "")

    total_raw = _structured_value(structured, {"TotalAmount", "AmountWithTax", "Jshj", "TaxInclusiveAmount"}) or _first_match(
        [r"(?:价税合计|小写|合计金额|Amount\s*with\s*tax)[^\d¥￥]*[¥￥]?\s*([0-9,]+\.\d{2})"], text, re.I
    )
    tax_raw = _structured_value(structured, {"TotalTaxAmount", "TaxAmount", "Hjse"}) or _first_match(
        [r"(?:税额合计|合计税额|税额)[^\d¥￥]*[¥￥]?\s*([0-9,]+\.\d{2})"], text, re.I
    )
    amount_raw = _structured_value(structured, {"Amount", "AmountWithoutTax", "Hjje"}) or _first_match(
        [r"(?:金额合计|不含税金额|合计金额)[^\d¥￥]*[¥￥]?\s*([0-9,]+\.\d{2})"], text, re.I
    )

    seller_name = _structured_value(structured, {"SellerName", "SalerName", "Xfmc"}) or _first_match(
        [r"(?:销售方|销方)(?:信息)?[\s\S]{0,40}?名称[：:\s]*([^\n]{2,80})", r"销售方名称[：:\s]*([^\n]{2,80})"], text
    )
    buyer_name = _structured_value(structured, {"BuyerName", "Gfmc"}) or _first_match(
        [r"(?:购买方|购方)(?:信息)?[\s\S]{0,40}?名称[：:\s]*([^\n]{2,80})", r"购买方名称[：:\s]*([^\n]{2,80})"], text
    )
    tax_ids = re.findall(r"(?:纳税人识别号|统一社会信用代码)[：:\s]*([0-9A-Z]{15,20})", compact, re.I)
    buyer_tax_id = _structured_value(structured, {"BuyerTaxNo", "BuyerTaxId", "Gfsh"}) or (tax_ids[0] if tax_ids else "")
    seller_tax_id = _structured_value(structured, {"SellerTaxNo", "SalerTaxNo", "SellerTaxId", "Xfsh"}) or (
        tax_ids[-1] if len(tax_ids) > 1 else ""
    )

    invoice_type = _structured_value(structured, {"InvoiceTypeName", "InvoiceType"})
    if not invoice_type:
        invoice_type = _first_match([r"(增值税(?:电子)?(?:专用|普通)发票|数电发票|电子发票|铁路电子客票|航空运输电子客票)"], text)

    category = categorize(text, seller_name)
    return {
        "invoice_type": invoice_type,
        "invoice_code": invoice_code,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "check_code": check_code,
        "seller_name": seller_name[:255],
        "seller_tax_id": seller_tax_id,
        "buyer_name": buyer_name[:255],
        "buyer_tax_id": buyer_tax_id,
        "amount": _number(amount_raw),
        "tax_amount": _number(tax_raw),
        "total_amount": _number(total_raw),
        "category": category,
    }


def categorize(text: str, seller: str = "") -> str:
    source = f"{text}\n{seller}".lower()
    rules = [
        ("交通出行", ["滴滴", "铁路", "客票", "航空", "机票", "出租车", "通行费", "高速公路"]),
        ("住宿", ["酒店", "宾馆", "住宿服务", "旅馆"]),
        ("餐饮", ["餐饮", "餐费", "食品", "饭店", "餐厅"]),
        ("办公用品", ["办公用品", "文具", "打印耗材", "电脑", "电子产品"]),
        ("通讯", ["通信", "电信", "移动", "联通", "话费"]),
        ("物流", ["快递", "物流", "运输服务"]),
    ]
    for category, keywords in rules:
        if any(keyword.lower() in source for keyword in keywords):
            return category
    return "未分类"


def image_to_jpeg_bytes(image: Image.Image, max_side: int = 2200) -> bytes:
    copy = image.copy()
    copy.thumbnail((max_side, max_side))
    buffer = BytesIO()
    copy.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()

