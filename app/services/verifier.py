from __future__ import annotations

import base64
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
from app.models import Invoice, JobLog
from app.services.extractor import (
    FIELDS,
    extract_document,
    image_to_jpeg_bytes,
    parse_invoice_fields,
)
from app.services.settings_service import as_bool, get_integrations

logger = logging.getLogger(__name__)
_kingdee_token: dict[str, Any] = {"value": "", "expires": 0.0, "fingerprint": ""}


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
    return map_external_fields(invoice_data), raw


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
        db.commit()
        path = settings.upload_dir / invoice.stored_name
        config = get_integrations(db)
        kingdee_error = ""
        try:
            if _kingdee_complete(config):
                try:
                    fields, raw = verify_with_kingdee(path, config)
                    _apply_fields(invoice, fields)
                    invoice.kingdee_data = raw
                    invoice.verification_method = "kingdee"
                    invoice.status = "verified"
                    invoice.confidence = 1.0
                    invoice.verified_at = datetime.now(UTC).replace(tzinfo=None)
                except Exception as exc:
                    kingdee_error = str(exc)
                    db.add(JobLog(level="warning", event="kingdee.fallback", message=kingdee_error, details={"invoice_id": invoice.id}))

            if invoice.status != "verified":
                text, structured, preview = extract_document(path, invoice.mime_type)
                invoice.raw_text = text[:200000]
                ocr_fields = parse_invoice_fields(text, structured)
                invoice.ocr_data = ocr_fields
                llm_enabled = as_bool(config.get("llm_enabled")) and bool(config.get("llm_base_url") and config.get("llm_model"))
                if llm_enabled:
                    try:
                        preview_bytes = image_to_jpeg_bytes(preview) if preview else None
                        llm_fields = extract_with_llm(text, preview_bytes, config)
                        invoice.llm_data = llm_fields
                        merged, conflicts, confidence, consistent = compare_sources(ocr_fields, llm_fields)
                        _apply_fields(invoice, merged)
                        invoice.conflicts = conflicts
                        invoice.confidence = confidence
                        invoice.verification_method = "dual"
                        invoice.status = "consistent" if consistent else "review"
                        if consistent:
                            invoice.verified_at = datetime.now(UTC).replace(tzinfo=None)
                    except Exception as exc:
                        _apply_fields(invoice, ocr_fields)
                        invoice.verification_method = "ocr"
                        invoice.status = "review"
                        invoice.error_message = f"LLM 提取失败：{exc}"
                else:
                    _apply_fields(invoice, ocr_fields)
                    invoice.verification_method = "ocr"
                    invoice.status = "review"
                    invoice.error_message = "未配置 LLM，已完成本地 OCR/文本提取，需人工复核"
                if kingdee_error:
                    invoice.error_message = f"金蝶查验未完成：{kingdee_error}；已回退到 {invoice.verification_method.upper()}"

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


def test_llm(config: dict[str, str]) -> str:
    if not as_bool(config.get("llm_enabled")):
        raise IntegrationError("请先启用 LLM")
    result = extract_with_llm("发票号码：12345678\n开票日期：2026-01-02\n价税合计：100.00", None, config)
    if not result.get("invoice_number"):
        raise IntegrationError("LLM 已响应，但未按要求返回结构化字段")
    return f"连接成功，模型返回测试票号 {result['invoice_number']}"

