from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import and_, or_, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Invoice, JobLog, User
from app.services.extractor import (
    FIELDS,
    categorize,
    extract_document,
    image_to_jpeg_bytes,
    parse_invoice_fields,
)
from app.services.settings_service import as_bool, get_integrations
from app.services.title_service import title_warning

logger = logging.getLogger(__name__)
_kingdee_token: dict[str, Any] = {"value": "", "expires": 0.0, "fingerprint": ""}
_piaozone_token: dict[str, Any] = {"value": "", "expires": 0.0, "fingerprint": ""}


class IntegrationError(RuntimeError):
    pass


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in {"true", "1", "success", "ok"}


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    try:
        padding = "=" * (-len(stripped) % 4)
        decoded = base64.b64decode(stripped + padding).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return value


def _unwrap_invoice(payload: Any) -> dict[str, Any]:
    payload = _jsonish(payload)
    if isinstance(payload, list):
        for item in payload:
            result = _unwrap_invoice(item)
            if result:
                return result
        return {}
    if not isinstance(payload, dict):
        return {}
    for key in ("invoiceList", "invoices", "invoice", "result", "data"):
        if key in payload:
            nested = _unwrap_invoice(payload[key])
            if nested:
                return nested
    recognizable = {"invoiceNo", "invoiceNumber", "invoiceCode", "totalAmount", "salerName", "sellerName"}
    if recognizable.intersection(payload):
        return payload
    return payload if len(payload) > 5 else {}


def _pick(data: dict[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return ""


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def map_external_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice_type": str(_pick(data, "invoiceTypeName", "invoiceType", "type")),
        "invoice_code": str(_pick(data, "invoiceCode", "code", "fpdm")),
        "invoice_number": str(_pick(data, "invoiceNo", "invoiceNumber", "number", "fphm")),
        "invoice_date": str(_pick(data, "invoiceDate", "issueDate", "date", "kprq"))[:20],
        "check_code": str(_pick(data, "checkCode", "jym")),
        "seller_name": str(_pick(data, "salerName", "sellerName", "xfmc"))[:255],
        "seller_tax_id": str(_pick(data, "salerTaxNo", "sellerTaxNo", "sellerTaxId", "xfsh")),
        "buyer_name": str(_pick(data, "buyerName", "gfmc"))[:255],
        "buyer_tax_id": str(_pick(data, "buyerTaxNo", "buyerTaxId", "gfsh")),
        "amount": _float(_pick(data, "amount", "invoiceAmount", "amountWithoutTax", "hjje")),
        "tax_amount": _float(_pick(data, "taxAmount", "totalTaxAmount", "hjse")),
        "total_amount": _float(_pick(data, "totalAmount", "amountWithTax", "jshj")),
        "category": str(_pick(data, "category")) or "未分类",
    }


def _kingdee_complete(config: dict[str, str]) -> bool:
    required = ["kingdee_base_url", "kingdee_app_id", "kingdee_app_secret", "kingdee_account_id", "kingdee_user"]
    return as_bool(config.get("kingdee_enabled")) and all(config.get(key) for key in required)


def get_kingdee_access_token(config: dict[str, str], force: bool = False) -> str:
    fingerprint = "|".join(config.get(key, "") for key in ("kingdee_base_url", "kingdee_app_id", "kingdee_account_id", "kingdee_user"))
    if not force and _kingdee_token["value"] and _kingdee_token["expires"] > time.time() + 60 and _kingdee_token["fingerprint"] == fingerprint:
        return str(_kingdee_token["value"])
    base_url = config["kingdee_base_url"].rstrip("/")
    with httpx.Client(timeout=30.0, verify=True) as client:
        app_response = client.post(
            f"{base_url}/api/getAppToken.do",
            json={
                "appId": config["kingdee_app_id"],
                "appSecret": config["kingdee_app_secret"],
                "accountId": config["kingdee_account_id"],
                "tenantid": config.get("kingdee_tenant_id", "1"),
                "language": "zh_CN",
            },
        )
        app_response.raise_for_status()
        app_payload = app_response.json()
        app_data = app_payload.get("data", {})
        app_token = app_data.get("app_token")
        if not app_token or not _truthy(app_data.get("success", app_payload.get("status"))):
            raise IntegrationError(app_data.get("error_desc") or "金蝶 app_token 获取失败")
        login_response = client.post(
            f"{base_url}/api/login.do",
            json={"user": config["kingdee_user"], "apptoken": app_token, "accountId": config["kingdee_account_id"]},
        )
        login_response.raise_for_status()
        login_payload = login_response.json()
        login_data = login_payload.get("data", {})
        access_token = login_data.get("access_token")
        if not access_token or not _truthy(login_data.get("success", login_payload.get("status"))):
            raise IntegrationError(login_data.get("error_desc") or "金蝶 access_token 获取失败")
    expires_ms = login_data.get("expire_time")
    expires = float(expires_ms) / 1000 if expires_ms else time.time() + 7000
    _kingdee_token.update(value=access_token, expires=expires, fingerprint=fingerprint)
    return str(access_token)


def verify_with_kingdee(path: Path, config: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    access_token = get_kingdee_access_token(config)
    base_url = config["kingdee_base_url"].rstrip("/")
    extension = path.suffix.lower().lstrip(".")
    payload = {
        "messageType": "recognitionCheck",
        "messageId": str(uuid4()),
        "data": {
            "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "fileDownUrl": "",
            "fileType": extension,
            "verifyFlag": "1",
            "notCheck": "0",
            "billType": "",
            "billId": "",
            "orgNumber": config.get("kingdee_org_number", ""),
            "taxNo": config.get("kingdee_tax_no", ""),
            "resource": "INVOICEDOCK",
            "companyInfo": [
                {"taxNo": config.get("kingdee_tax_no", ""), "name": config.get("kingdee_company_name", "")}
            ] if config.get("kingdee_tax_no") or config.get("kingdee_company_name") else [],
            "salerInfo": [],
        },
    }
    with httpx.Client(timeout=90.0, verify=True) as client:
        response = client.post(
            f"{base_url}/kapi/app/rim/message",
            params={"access_token": access_token},
            headers={"access_token": access_token},
            json=payload,
        )
        response.raise_for_status()
        raw = response.json()
    if not _truthy(raw.get("success")):
        raise IntegrationError(str(raw.get("message") or raw.get("errorCode") or "金蝶识别查验失败"))
    invoice_data = _unwrap_invoice(raw.get("data"))
    if not invoice_data:
        raise IntegrationError("金蝶返回成功但未包含可识别的发票字段")
    fields = map_external_fields(invoice_data)
    if not _provider_fields_valid(fields):
        raise IntegrationError("金蝶旗舰版未识别到有效发票字段（缺少发票号码、金额或购销方）")
    return fields, raw


def _piaozone_complete(config: dict[str, str]) -> bool:
    required = ["piaozone_base_url", "piaozone_client_id", "piaozone_client_secret"]
    return as_bool(config.get("piaozone_enabled")) and all(config.get(key) for key in required)


def _provider_fields_valid(fields: dict[str, Any]) -> bool:
    """A verified invoice must carry real invoice identity: 发票号码, 金额,
    and at least one party. Providers return success with placeholder data
    for non-invoice documents (receipts, quotes, etc.), so presence of these
    core fields is required before marking an invoice as 税务已查验."""
    return bool(fields.get("invoice_number")) and fields.get("total_amount") is not None and bool(
        fields.get("seller_name") or fields.get("buyer_name")
    )


def has_invoice_identity(invoice: Invoice) -> bool:
    """A document is treated as an invoice when extraction produced at least
    one of: 发票号码 / 金额 / 销售方 / 购买方."""
    return bool(
        invoice.invoice_number
        or invoice.total_amount is not None
        or invoice.seller_name
        or invoice.buyer_name
    )


def _piaozone_sign(client_id: str, client_secret: str, timestamp: str, method: str) -> tuple[str, int]:
    text = f"{client_id}{client_secret}{timestamp}"
    method = (method or "MD5").upper()
    if method == "SHA256":
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), 1
    if method == "HMACSHA256":
        return hmac.new(client_secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest(), 2
    return hashlib.md5(text.encode("utf-8")).hexdigest(), 0


def get_piaozone_access_token(config: dict[str, str], force: bool = False) -> str:
    base_url = config["piaozone_base_url"].rstrip("/")
    token_path = config.get("piaozone_token_path") or "/base/oauth/token"
    fingerprint = "|".join([base_url, token_path, config.get("piaozone_client_id", "")])
    if not force and _piaozone_token["value"] and _piaozone_token["expires"] > time.time() + 60 and _piaozone_token["fingerprint"] == fingerprint:
        return str(_piaozone_token["value"])
    timestamp = str(int(time.time() * 1000))
    sign, enc_type = _piaozone_sign(
        config["piaozone_client_id"],
        config["piaozone_client_secret"],
        timestamp,
        config.get("piaozone_sign_method", "MD5"),
    )
    with httpx.Client(timeout=30.0, verify=True) as client:
        response = client.post(
            f"{base_url}{token_path}",
            json={
                "client_id": config["piaozone_client_id"],
                "timestamp": timestamp,
                "sign": sign,
                "encType": enc_type,
            },
        )
        response.raise_for_status()
        raw = response.json()
        token = ""
        for key in ("access_token", "accessToken", "token"):
            if raw.get(key):
                token = str(raw[key])
                break
        if not token and isinstance(raw.get("data"), dict):
            for key in ("access_token", "accessToken", "token"):
                if raw["data"].get(key):
                    token = str(raw["data"][key])
                    break
        if not token:
            raise IntegrationError(str(raw.get("message") or raw.get("error") or "金蝶标准版 access_token 获取失败"))
    _piaozone_token.update(value=token, expires=time.time() + 7000, fingerprint=fingerprint)
    return token


def _piaozone_nested(raw: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if any(key in value for key in ("invoiceNo", "InvoiceNo", "buyerName", "BuyerName", "totalAmount", "TotalAmount", "salerName")):
                candidates.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(raw)
    return candidates[0] if candidates else (raw if isinstance(raw, dict) else {})


def _piaozone_date(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(\d{4})[-年./](\d{1,2})[-月./](\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    return text[:20]


def verify_with_piaozone(path: Path, config: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    token = get_piaozone_access_token(config)
    base_url = config["piaozone_base_url"].rstrip("/")
    check_path = config.get("piaozone_invoice_check_path") or "/m3/bill/invoice/img/Check/info"
    extension = path.suffix.lower().lstrip(".")
    file_type = "jpg" if extension in {"jpg", "jpeg"} else (extension or "pdf")
    body = base64.b64encode(path.read_bytes()).decode("ascii")
    with httpx.Client(timeout=90.0, verify=True) as client:
        response = client.post(
            f"{base_url}{check_path}",
            params={"access_token": token, "type": file_type},
            content=body,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        raw = response.json()
    payload = _piaozone_nested(raw)
    if not payload:
        raise IntegrationError("金蝶标准版返回成功但未包含可识别的发票字段")
    fields = {
        "invoice_type": str(_pick(payload, "invoiceTypeName", "invoiceType", "type")),
        "invoice_code": str(_pick(payload, "invoiceCode", "code", "fpdm")),
        "invoice_number": str(_pick(payload, "invoiceNo", "InvoiceNo", "invoiceNumber", "digitalInvoiceNo", "eInvoiceNo", "number", "fphm")),
        "invoice_date": _piaozone_date(_pick(payload, "invoiceDate", "issueDate", "billingDate", "date", "kprq")),
        "check_code": str(_pick(payload, "checkCode", "jym")),
        "seller_name": str(_pick(payload, "salerName", "sellerName", "salesName", "xfmc"))[:255],
        "seller_tax_id": str(_pick(payload, "salerTaxNo", "sellerTaxNo", "sellerNsrsbh", "xfsh")),
        "buyer_name": str(_pick(payload, "buyerName", "purchaserName", "buyer", "gfmc"))[:255],
        "buyer_tax_id": str(_pick(payload, "buyerTaxNo", "buyerNsrsbh", "gfsh")),
        "amount": _float(_pick(payload, "amountWithoutTax", "amount", "hjje")),
        "tax_amount": _float(_pick(payload, "taxAmount", "hjse")),
        "total_amount": _float(_pick(payload, "totalAmount", "amountWithTax", "totalTaxAmount", "invoiceAmount", "jshj")),
        "category": "未分类",
    }
    if not _provider_fields_valid(fields):
        raise IntegrationError("金蝶标准版未识别到有效发票字段（缺少发票号码、金额或购销方）")
    return fields, raw


LLM_PROMPT = """你是中国发票结构化提取器。只依据提供的原文和图片，不猜测看不清的值。
返回一个 JSON 对象，不要 Markdown，不要解释。字段必须完整：
invoice_type, invoice_code, invoice_number, invoice_date(YYYY-MM-DD), check_code,
seller_name, seller_tax_id, buyer_name, buyer_tax_id,
amount(不含税金额，数字或null), tax_amount(数字或null), total_amount(价税合计，数字或null),
category(交通出行/住宿/餐饮/办公用品/通讯/物流/未分类之一)。
缺失字符串填空字符串，缺失数字填 null。务必区分购买方和销售方。"""


def extract_with_llm(text: str, preview_bytes: bytes | None, config: dict[str, str]) -> dict[str, Any]:
    base_url = config["llm_base_url"].rstrip("/")
    user_text = f"请提取这张发票。OCR/文本层内容如下：\n{text[:18000]}"
    content: Any = user_text
    if preview_bytes and as_bool(config.get("llm_vision", "true")):
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(preview_bytes).decode('ascii')}"}},
        ]
    request_body = {
        "model": config["llm_model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": LLM_PROMPT},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if config.get("llm_api_key"):
        headers["Authorization"] = f"Bearer {config['llm_api_key']}"
    with httpx.Client(timeout=120.0) as client:
        response = client.post(f"{base_url}/chat/completions", headers=headers, json=request_body)
        response.raise_for_status()
        payload = response.json()
    try:
        raw_content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise IntegrationError("LLM 返回结构不兼容 OpenAI Chat Completions") from exc
    if isinstance(raw_content, list):
        raw_content = "".join(str(item.get("text", "")) for item in raw_content if isinstance(item, dict))
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw_content).strip(), flags=re.I)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise IntegrationError("LLM 未返回有效 JSON") from exc
    return map_external_fields(parsed)


def _normalize(field: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if field in {"amount", "tax_amount", "total_amount"}:
        return _float(value)
    if field == "invoice_date":
        return re.sub(r"\D", "", str(value))
    if field in {"seller_name", "buyer_name"}:
        return re.sub(r"[\s（）()·,，。.]", "", str(value)).upper()
    return re.sub(r"\s", "", str(value)).upper()


def compare_sources(ocr: dict[str, Any], llm: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], float, bool]:
    merged: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    matched = 0
    comparable = 0
    for field in FIELDS:
        left = ocr.get(field)
        right = llm.get(field)
        merged[field] = left if left not in (None, "", "未分类") else right
        left_n = _normalize(field, left)
        right_n = _normalize(field, right)
        if left_n is None or right_n is None:
            continue
        comparable += 1
        equal = abs(left_n - right_n) <= 0.01 if isinstance(left_n, float) and isinstance(right_n, float) else left_n == right_n
        if equal:
            matched += 1
        else:
            conflicts.append({"field": field, "ocr": left, "llm": right})
    confidence = matched / comparable if comparable else 0.0
    critical = {"invoice_number", "invoice_date", "total_amount"}
    critical_conflicts = {item["field"] for item in conflicts}.intersection(critical)
    critical_present = sum(1 for field in critical if merged.get(field) not in (None, "")) >= 2
    consistent = comparable >= 3 and not critical_conflicts and critical_present and confidence >= 0.75
    return merged, conflicts, round(confidence, 3), consistent


def _apply_fields(invoice: Invoice, fields: dict[str, Any]) -> None:
    for field in FIELDS:
        if field in fields and fields[field] is not None:
            setattr(invoice, field, fields[field])


def _find_business_duplicate(invoice: Invoice, db) -> Invoice | None:  # type: ignore[no-untyped-def]
    if not invoice.invoice_number:
        return None
    filters = [Invoice.id != invoice.id, Invoice.invoice_number == invoice.invoice_number]
    if invoice.invoice_code:
        filters.append(Invoice.invoice_code == invoice.invoice_code)
    else:
        filters.append(or_(Invoice.invoice_date == invoice.invoice_date, Invoice.total_amount == invoice.total_amount))
    return db.scalar(select(Invoice).where(and_(*filters)).order_by(Invoice.created_at.asc()).limit(1))


def process_invoice(invoice_id: str) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        invoice = db.get(Invoice, invoice_id)
        if not invoice:
            return
        invoice.status = "processing"
        invoice.error_message = ""
        invoice.title_warning = ""
        db.commit()
        path = settings.upload_dir / invoice.stored_name
        config = get_integrations(db, user_id=invoice.owner_id)
        provider_allowed = as_bool(config.get("verify_provider", "true"))
        ocr_allowed = as_bool(config.get("verify_ocr", "true"))
        llm_allowed = as_bool(config.get("verify_llm", "true"))
        provider_error = ""
        try:
            if provider_allowed and _piaozone_complete(config):
                try:
                    fields, raw = verify_with_piaozone(path, config)
                    _apply_fields(invoice, fields)
                    invoice.kingdee_data = raw
                    invoice.verification_method = "piaozone"
                    invoice.status = "verified"
                    invoice.confidence = 1.0
                    invoice.verified_at = datetime.now(UTC).replace(tzinfo=None)
                except Exception as exc:
                    provider_error = str(exc)
                    db.add(JobLog(level="warning", event="piaozone.fallback", message=provider_error, details={"invoice_id": invoice.id}))

            if provider_allowed and invoice.status != "verified" and _kingdee_complete(config):
                try:
                    fields, raw = verify_with_kingdee(path, config)
                    _apply_fields(invoice, fields)
                    invoice.kingdee_data = raw
                    invoice.verification_method = "kingdee"
                    invoice.status = "verified"
                    invoice.confidence = 1.0
                    invoice.verified_at = datetime.now(UTC).replace(tzinfo=None)
                except Exception as exc:
                    provider_error = str(exc)
                    db.add(JobLog(level="warning", event="kingdee.fallback", message=provider_error, details={"invoice_id": invoice.id}))

            if invoice.status != "verified":
                if not (ocr_allowed or llm_allowed):
                    invoice.verification_method = ""
                    invoice.status = "review"
                    invoice.error_message = "发票云查验未完成，且已禁用本地 OCR/LLM 回退，请人工处理"
                else:
                    text, structured, preview = extract_document(path, invoice.mime_type)
                    invoice.raw_text = text[:200000]
                    ocr_fields = parse_invoice_fields(text, structured) if ocr_allowed else {}
                    invoice.ocr_data = ocr_fields
                    llm_configured = as_bool(config.get("llm_enabled")) and bool(config.get("llm_base_url") and config.get("llm_model"))
                    if llm_allowed and llm_configured:
                        try:
                            preview_bytes = image_to_jpeg_bytes(preview) if preview else None
                            llm_fields = extract_with_llm(text, preview_bytes, config)
                            invoice.llm_data = llm_fields
                            if ocr_allowed:
                                merged, conflicts, confidence, consistent = compare_sources(ocr_fields, llm_fields)
                                _apply_fields(invoice, merged)
                                invoice.conflicts = conflicts
                                invoice.confidence = confidence
                                invoice.verification_method = "dual"
                                invoice.status = "consistent" if consistent else "review"
                                if consistent:
                                    invoice.verified_at = datetime.now(UTC).replace(tzinfo=None)
                            else:
                                _apply_fields(invoice, llm_fields)
                                invoice.verification_method = "llm"
                                invoice.status = "review"
                        except Exception as exc:
                            if ocr_allowed:
                                _apply_fields(invoice, ocr_fields)
                                invoice.verification_method = "ocr"
                                invoice.status = "review"
                                invoice.error_message = f"LLM 提取失败：{exc}"
                            else:
                                invoice.verification_method = ""
                                invoice.status = "review"
                                invoice.error_message = f"LLM 提取失败：{exc}"
                    elif ocr_allowed:
                        _apply_fields(invoice, ocr_fields)
                        invoice.verification_method = "ocr"
                        invoice.status = "review"
                        invoice.error_message = "未配置 LLM，已完成本地 OCR/文本提取，需人工复核"
                    else:
                        invoice.verification_method = ""
                        invoice.status = "review"
                        invoice.error_message = "LLM 未配置且已禁用本地 OCR，请人工处理"
                if provider_error:
                    fallback_label = invoice.verification_method.upper() if invoice.verification_method else "人工复核"
                    invoice.error_message = f"金蝶查验未完成：{provider_error}；已回退到 {fallback_label}"

            if invoice.category in ("", "未分类"):
                invoice.category = categorize(
                    f"{invoice.original_name}\n{invoice.invoice_type}", invoice.seller_name
                ) or "未分类"
            owner = db.get(User, invoice.owner_id) if invoice.owner_id else None
            invoice.title_warning = title_warning(db, owner, invoice.buyer_name, invoice.buyer_tax_id)
            duplicate = _find_business_duplicate(invoice, db)
            if duplicate:
                invoice.duplicate_of = duplicate.id
                invoice.status = "duplicate"
            db.add(
                JobLog(
                    event="invoice.processed",
                    message=f"{invoice.original_name} 处理完成：{invoice.status}",
                    details={"invoice_id": invoice.id, "method": invoice.verification_method},
                )
            )
            db.commit()
        except Exception as exc:
            logger.exception("Invoice processing failed: %s", invoice_id)
            invoice.status = "failed"
            invoice.error_message = str(exc)[:1000]
            db.add(JobLog(level="error", event="invoice.failed", message=str(exc)[:1000], details={"invoice_id": invoice.id}))
            db.commit()


def test_kingdee(config: dict[str, str]) -> str:
    if not _kingdee_complete(config):
        raise IntegrationError("请先启用并填写完整的金蝶连接信息")
    token = get_kingdee_access_token(config, force=True)
    return f"连接成功，access_token 已获取（…{token[-8:]}）"


def test_piaozone(config: dict[str, str]) -> str:
    if not _piaozone_complete(config):
        raise IntegrationError("请先启用并填写完整的金蝶标准版（Piaozone）连接信息")
    token = get_piaozone_access_token(config, force=True)
    return f"连接成功，access_token 已获取（…{token[-8:]}）"


def test_llm(config: dict[str, str]) -> str:
    if not as_bool(config.get("llm_enabled")):
        raise IntegrationError("请先启用 LLM")
    result = extract_with_llm("发票号码：12345678\n开票日期：2026-01-02\n价税合计：100.00", None, config)
    if not result.get("invoice_number"):
        raise IntegrationError("LLM 已响应，但未按要求返回结构化字段")
    return f"连接成功，模型返回测试票号 {result['invoice_number']}"
