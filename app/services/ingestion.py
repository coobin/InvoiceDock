from __future__ import annotations

import hashlib
import re
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Invoice, JobLog

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".xml", ".ofd"}
ARCHIVE_EXTENSIONS = {".zip"}


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


def ingest_bytes(
    db: Session,
    data: bytes,
    filename: str,
    source: str = "upload",
    source_ref: str = "",
    owner_id: str | None = None,
) -> tuple[Invoice, bool]:
    name, mime = validate_file(data, filename)
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(Invoice).where(Invoice.sha256 == digest))
    if existing:
        return existing, False

    extension = Path(name).suffix.lower()
    stored_name = f"{uuid4()}{extension}"
    target = get_settings().upload_dir / stored_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

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
        existing = db.scalar(select(Invoice).where(Invoice.sha256 == digest))
        if existing:
            return existing, False
        raise
    db.refresh(invoice)
    return invoice, True


def extract_zip_candidates(data: bytes, max_files: int = 30, max_uncompressed_mb: int = 80) -> list[tuple[str, bytes]]:
    results: list[tuple[str, bytes]] = []
    total = 0
    with zipfile.ZipFile(BytesIO(data)) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) > max_files:
            raise ValueError(f"压缩包文件数超过 {max_files} 个")
        for info in infos:
            total += info.file_size
            if total > max_uncompressed_mb * 1024 * 1024:
                raise ValueError("压缩包解压后体积过大")
            extension = Path(info.filename).suffix.lower()
            if extension in ALLOWED_EXTENSIONS:
                results.append((safe_filename(info.filename), archive.read(info)))
    return results
