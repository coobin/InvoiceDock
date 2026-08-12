from __future__ import annotations

import email
import imaplib
import ipaddress
import logging
import re
import socket
import ssl
from email.header import decode_header
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Invoice, JobLog, Mailbox, ProcessedEmail, utcnow
from app.security import decrypt_secret
from app.services.ingestion import ALLOWED_EXTENSIONS, extract_zip_candidates, ingest_bytes
from app.services.settings_service import get_integrations
from app.services.verifier import has_invoice_identity, process_invoice, provider_configured

logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
LINK_HINTS = ("invoice", "fapiao", "piao", "pdf", "ofd", "xml", "download", "%E5%8F%91%E7%A5%A8")


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    pieces: list[str] = []
    for item, charset in decode_header(value):
        if isinstance(item, bytes):
            try:
                pieces.append(item.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                pieces.append(item.decode("utf-8", errors="replace"))
        else:
            pieces.append(item)
    return "".join(pieces)


def _connect(mailbox: Mailbox) -> imaplib.IMAP4:
    password = decrypt_secret(mailbox.password_encrypted)
    if not password:
        raise RuntimeError("邮箱密码无法解密，请重新保存邮箱配置")
    if mailbox.use_ssl:
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            mailbox.host, mailbox.port, ssl_context=ssl.create_default_context(), timeout=25
        )
    else:
        client = imaplib.IMAP4(mailbox.host, mailbox.port, timeout=25)
        client.starttls(ssl_context=ssl.create_default_context())
    client.login(mailbox.username, password)
    status, _ = client.select(mailbox.folder, readonly=True)
    if status != "OK":
        client.logout()
        raise RuntimeError(f"无法打开邮箱文件夹 {mailbox.folder}")
    return client


def test_mailbox(mailbox: Mailbox) -> str:
    client = _connect(mailbox)
    try:
        status, data = client.status(mailbox.folder, "(MESSAGES UNSEEN UIDNEXT)")
        if status != "OK":
            raise RuntimeError("邮箱已登录，但无法读取文件夹状态")
        return decode_mime_header(data[0].decode(errors="replace") if data and data[0] else "连接成功")
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _email_bodies(message: Message) -> list[str]:
    bodies: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            bodies.append(raw.decode(charset, errors="replace"))
        except LookupError:
            bodies.append(raw.decode("utf-8", errors="replace"))
    return bodies


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许 HTTP/HTTPS 公网链接")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError("链接域名无法解析") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError("链接指向内网或保留地址，已阻止")


def fetch_direct_invoice(url: str) -> tuple[str, bytes] | None:
    settings = get_settings()
    current = url.rstrip(".,);]}>\"")
    max_bytes = settings.max_upload_mb * 1024 * 1024
    with httpx.Client(timeout=25.0, headers={"User-Agent": "InvoiceDock/0.1 invoice collector"}) as client:
        for _ in range(5):
            _assert_public_url(current)
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_length = int(response.headers.get("content-length", "0") or 0)
                if content_length > max_bytes:
                    raise ValueError("邮件链接文件超过上传限制")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("邮件链接文件超过上传限制")
                    chunks.append(chunk)
                data = b"".join(chunks)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                path_name = Path(unquote(urlparse(str(response.url)).path)).name
                disposition = response.headers.get("content-disposition", "")
                match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
                filename = unquote(match.group(1)) if match else path_name
                extension = Path(filename).suffix.lower()
                if data.startswith(b"%PDF") and extension != ".pdf":
                    filename = f"{filename or 'email-invoice'}.pdf"
                elif extension not in ALLOWED_EXTENSIONS and content_type not in {
                    "application/pdf", "application/xml", "image/png", "image/jpeg"
                }:
                    return None
                return filename or "email-invoice.pdf", data
        return None


def _attachment_candidates(message: Message) -> list[tuple[str, bytes]]:
    candidates: list[tuple[str, bytes]] = []
    for part in message.walk():
        filename = decode_mime_header(part.get_filename())
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        extension = Path(filename).suffix.lower()
        if extension in ALLOWED_EXTENSIONS:
            candidates.append((filename, payload))
        elif extension == ".zip":
            try:
                candidates.extend(extract_zip_candidates(payload))
            except Exception as exc:
                logger.warning("Skipped unsafe or invalid zip attachment %s: %s", filename, exc)
    return candidates


def _remove_email_invoice(db: Session, invoice, event: str, message: str) -> None:
    """Delete an email-imported invoice record plus its local files."""
    settings = get_settings()
    original = settings.upload_dir / invoice.stored_name
    preview = settings.preview_dir / f"{invoice.id}.jpg"
    db.add(JobLog(user_id=getattr(invoice, "owner_id", None), level="warning", event=event, message=message, details={"invoice_id": invoice.id}))
    db.delete(invoice)
    db.commit()
    original.unlink(missing_ok=True)
    preview.unlink(missing_ok=True)


def _discard_non_invoice(db: Session, invoice) -> bool:
    """Email attachments that contain no invoice identity are discarded
    entirely (record + local files) so unrelated documents never enter the
    ledger. Returns True when the invoice was discarded."""
    if has_invoice_identity(invoice):
        return False
    _remove_email_invoice(
        db, invoice, "email.discard_non_invoice", f"{invoice.original_name} 未识别到发票要素，已自动丢弃"
    )
    return True


def _discard_not_cloud_verified(db: Session, invoice) -> None:
    """When 发票云 is configured, email-imported documents must pass cloud
    verification; receipts, quotes and OCR-only or failed documents are
    filtered out instead of entering the review queue."""
    _remove_email_invoice(
        db,
        invoice,
        "email.discard_unverified",
        f"{invoice.original_name} 税务发票云未能校验为有效发票，已自动过滤",
    )


def _dedupe_keep_pdf(db: Session, invoice) -> bool:
    """Duplicate email imports are collapsed to a single PDF record.

    - non-PDF duplicates are discarded;
    - a PDF duplicate replaces an existing non-PDF record so only the PDF
      remains in the ledger (the old record and its files are removed);
    - when the existing record is already a PDF, the extra copy is discarded.

    Returns True when the incoming record should remain in the ledger.
    """
    original = db.get(Invoice, invoice.duplicate_of) if invoice.duplicate_of else None
    if invoice.mime_type == "application/pdf":
        if original and original.mime_type != "application/pdf":
            # Release the self-referential foreign key before deleting the
            # non-PDF record. SQLite enforces the constraint immediately, so
            # deleting the original first fails while this PDF still points
            # at it as a duplicate.
            invoice.duplicate_of = None
            invoice.status = "verified" if invoice.verified_at else "review"
            db.commit()
            _remove_email_invoice(
                db,
                original,
                "email.dedupe_keep_pdf",
                f"{original.original_name} 已由 PDF 版本 {invoice.original_name} 替代，仅保留 PDF",
            )
            return True
        if original:
            _remove_email_invoice(
                db,
                invoice,
                "email.discard_duplicate",
                f"{invoice.original_name} 已有同名 PDF，重复副本已丢弃",
            )
            return False
        return True
    _remove_email_invoice(
        db,
        invoice,
        "email.discard_duplicate",
        f"{invoice.original_name} 与已有发票重复且非 PDF，已丢弃",
    )
    return False


def sync_mailbox(db: Session, mailbox: Mailbox) -> dict[str, int]:
    client = _connect(mailbox)
    imported = 0
    scanned = 0
    failed = 0
    max_uid = mailbox.last_uid
    provider_ready = provider_configured(get_integrations(db, user_id=mailbox.created_by))
    try:
        start_uid = max(1, mailbox.last_uid + 1)
        status, data = client.uid("search", None, f"UID {start_uid}:*")
        if status != "OK":
            raise RuntimeError("邮箱 UID 搜索失败")
        uid_values = (data[0] or b"").split()[: get_settings().mail_fetch_limit]
        for uid_bytes in uid_values:
            uid = int(uid_bytes)
            max_uid = max(max_uid, uid)
            if db.scalar(select(ProcessedEmail.id).where(ProcessedEmail.mailbox_id == mailbox.id, ProcessedEmail.uid == uid)):
                continue
            scanned += 1
            status, fetched = client.uid("fetch", uid_bytes, "(RFC822)")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                failed += 1
                continue
            message = email.message_from_bytes(fetched[0][1])
            subject = decode_mime_header(message.get("Subject"))
            sender = decode_mime_header(message.get("From"))
            record = ProcessedEmail(
                mailbox_id=mailbox.id,
                uid=uid,
                message_id=str(message.get("Message-ID", ""))[:500],
                subject=subject[:500],
                sender=sender[:500],
            )
            try:
                current_imported = 0
                for filename, payload in _attachment_candidates(message):
                    invoice, created = ingest_bytes(
                        db, payload, filename, source="email", source_ref=f"{mailbox.name} · {subject}",
                        owner_id=mailbox.created_by,
                    )
                    if created:
                        process_invoice(invoice.id)
                        db.expire_all()
                        fresh = db.get(Invoice, invoice.id)
                        if fresh and _discard_non_invoice(db, fresh):
                            continue
                        if fresh and fresh.status == "duplicate":
                            if not _dedupe_keep_pdf(db, fresh):
                                continue
                        elif fresh and provider_ready and fresh.status not in {"verified", "duplicate"}:
                            _discard_not_cloud_verified(db, fresh)
                            continue
                        current_imported += 1
                urls: set[str] = set()
                for body in _email_bodies(message):
                    urls.update(URL_RE.findall(body))
                for url in list(urls)[:20]:
                    if current_imported >= 10:
                        break
                    if not any(hint.lower() in url.lower() for hint in LINK_HINTS):
                        continue
                    try:
                        result = fetch_direct_invoice(url)
                        if not result:
                            continue
                        filename, payload = result
                        invoice, created = ingest_bytes(
                            db, payload, filename, source="email-link", source_ref=f"{mailbox.name} · {subject}",
                            owner_id=mailbox.created_by,
                        )
                        if created:
                            process_invoice(invoice.id)
                            db.expire_all()
                            fresh = db.get(Invoice, invoice.id)
                            if fresh and _discard_non_invoice(db, fresh):
                                continue
                            if fresh and fresh.status == "duplicate":
                                if not _dedupe_keep_pdf(db, fresh):
                                    continue
                            elif fresh and provider_ready and fresh.status not in {"verified", "duplicate"}:
                                _discard_not_cloud_verified(db, fresh)
                                continue
                            current_imported += 1
                    except Exception as exc:
                        logger.info("Invoice link not imported (%s): %s", url, exc)
                imported += current_imported
                record.imported_count = current_imported
                record.status = "imported" if current_imported else "no_invoice"
            except Exception as exc:
                failed += 1
                record.status = "failed"
                record.error_message = str(exc)[:1000]
            db.add(record)
            db.commit()
        mailbox.last_uid = max_uid
        mailbox.last_sync_at = utcnow()
        mailbox.last_error = ""
        db.add(JobLog(user_id=mailbox.created_by, event="mailbox.synced", message=f"{mailbox.name} 扫描 {scanned} 封，导入 {imported} 张", details={"mailbox_id": mailbox.id}))
        db.commit()
        return {"scanned": scanned, "imported": imported, "failed": failed}
    except Exception as exc:
        mailbox.last_sync_at = utcnow()
        mailbox.last_error = str(exc)[:1000]
        db.add(JobLog(user_id=mailbox.created_by, level="error", event="mailbox.failed", message=f"{mailbox.name}: {exc}", details={"mailbox_id": mailbox.id}))
        db.commit()
        raise
    finally:
        try:
            client.logout()
        except Exception:
            pass


def scan_all_mailboxes() -> None:
    with SessionLocal() as db:
        mailboxes = list(db.scalars(select(Mailbox).where(Mailbox.enabled.is_(True))).all())
        for mailbox in mailboxes:
            try:
                sync_mailbox(db, mailbox)
            except Exception:
                logger.exception("Scheduled mailbox sync failed: %s", mailbox.id)
