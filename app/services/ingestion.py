from __future__ import annotations

import hashlib
import re
import threading
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Invoice, JobLog
from app.services.quota_service import (
    ResourceLimitError,
    assert_user_storage_available,
    release_resource_usage,
    reserve_resource_usage,
)

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".xml", ".ofd"}
ARCHIVE_EXTENSIONS = {".zip"}
MAX_ARCHIVE_COMPRESSION_RATIO = 500
_ingest_lock = threading.RLock()


def safe_filename(name: str) -> str:
    clean = Path(name).name.replace("\x00", "")
    clean = re.sub(r"[^\w\-.（）()\u4e00-\u9fff ]+", "_", clean, flags=re.UNICODE).strip(" ._")
    return clean[:180] or "invoice"


def detect_mime(data: bytes, filename: str) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.lstrip().startswith((b"<?xml", b"<")):
        return "application/xml"
    if data.startswith(b"PK\x03\x04") and Path(filename).suffix.lower() == ".ofd":
        return "application/ofd"
    return "application/octet-stream"


def validate_file(data: bytes, filename: str) -> tuple[str, str]:
    settings = get_settings()
    if not data:
        raise ValueError("文件为空")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ValueError(f"文件超过 {settings.max_upload_mb} MB 限制")
    name = safe_filename(filename)
    extension = Path(name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持 {extension or '无扩展名'} 文件")
    mime = detect_mime(data, name)
    allowed_mimes = {"application/pdf", "image/png", "image/jpeg", "application/xml", "text/xml", "application/ofd"}
    if mime not in allowed_mimes:
        raise ValueError("文件内容与支持的发票格式不匹配")
    return name, mime


def _ingest_bytes_locked(
    db: Session,
    data: bytes,
    filename: str,
    source: str = "upload",
    source_ref: str = "",
    owner_id: str | None = None,
) -> tuple[Invoice, bool]:
    name, mime = validate_file(data, filename)
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(
        select(Invoice).where(Invoice.owner_id == owner_id, Invoice.sha256 == digest)
    )
    if existing:
        return existing, False

    assert_user_storage_available(db, owner_id, len(data))
    allowed, _used, daily_limit = reserve_resource_usage(db, owner_id, "upload")
    if not allowed:
        raise ResourceLimitError(f"今日上传数量已达到 {daily_limit} 个上限")

    extension = Path(name).suffix.lower()
    stored_name = f"{uuid4()}{extension}"
    target = get_settings().upload_dir / stored_name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_bytes(data)
    except Exception:
        release_resource_usage(db, owner_id, "upload")
        raise

    invoice = Invoice(
        original_name=name,
        stored_name=stored_name,
        mime_type=mime,
        file_size=len(data),
        sha256=digest,
        source=source,
        source_ref=source_ref[:500],
        owner_id=owner_id,
        status="pending",
    )
    db.add(invoice)
    db.add(JobLog(user_id=owner_id, event="invoice.ingested", message=f"已接收 {name}", details={"source": source}))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        target.unlink(missing_ok=True)
        release_resource_usage(db, owner_id, "upload")
        existing = db.scalar(
            select(Invoice).where(Invoice.owner_id == owner_id, Invoice.sha256 == digest)
        )
        if existing:
            return existing, False
        raise
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        release_resource_usage(db, owner_id, "upload")
        raise
    db.refresh(invoice)
    return invoice, True


def ingest_bytes(
    db: Session,
    data: bytes,
    filename: str,
    source: str = "upload",
    source_ref: str = "",
    owner_id: str | None = None,
) -> tuple[Invoice, bool]:
    """Ingest atomically within one application process.

    The lock closes the gap between checking per-user storage, writing the
    source file and committing its database row. InvoiceDock intentionally
    runs a single process with SQLite; multi-process deployments require a
    database-backed reservation instead.
    """
    with _ingest_lock:
        return _ingest_bytes_locked(
            db,
            data,
            filename,
            source=source,
            source_ref=source_ref,
            owner_id=owner_id,
        )


def extract_zip_candidates(
    data: bytes,
    max_files: int | None = None,
    max_uncompressed_mb: int | None = None,
) -> list[tuple[str, bytes]]:
    settings = get_settings()
    configured_files = max_files if max_files is not None else settings.max_archive_files
    configured_size = (
        max_uncompressed_mb
        if max_uncompressed_mb is not None
        else settings.max_archive_uncompressed_mb
    )
    max_candidate_bytes = settings.max_upload_mb * 1024 * 1024
    if len(data) > max_candidate_bytes:
        raise ValueError(f"压缩包超过 {settings.max_upload_mb} MB 上传限制")
    results: list[tuple[str, bytes]] = []
    total = 0
    with zipfile.ZipFile(BytesIO(data)) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) > configured_files:
            raise ValueError(f"压缩包文件数超过 {configured_files} 个")
        for info in infos:
            if info.flag_bits & 0x1:
                raise ValueError("不支持加密压缩包")
            total += info.file_size
            if total > configured_size * 1024 * 1024:
                raise ValueError("压缩包解压后体积过大")
            if info.file_size and (
                info.compress_size <= 0
                or info.file_size / info.compress_size > MAX_ARCHIVE_COMPRESSION_RATIO
            ):
                raise ValueError("压缩包包含异常高压缩比文件")
            extension = Path(info.filename).suffix.lower()
            if extension in ALLOWED_EXTENSIONS:
                if info.file_size > max_candidate_bytes:
                    raise ValueError(
                        f"压缩包内文件超过 {settings.max_upload_mb} MB 上传限制"
                    )
                with archive.open(info) as stream:
                    payload = stream.read(max_candidate_bytes + 1)
                if len(payload) > max_candidate_bytes:
                    raise ValueError(
                        f"压缩包内文件超过 {settings.max_upload_mb} MB 上传限制"
                    )
                results.append((safe_filename(info.filename), payload))
    return results
