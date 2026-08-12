from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Invoice, ResourceUsage, TaxVerificationUsage, utcnow
from app.services.settings_service import get_value, set_value

TAX_VERIFY_DAILY_LIMIT_KEY = "tax_verify_daily_limit"
DEFAULT_TAX_VERIFY_DAILY_LIMIT = 50
MIN_TAX_VERIFY_DAILY_LIMIT = 1
MAX_TAX_VERIFY_DAILY_LIMIT = 10000
RESOURCE_LIMIT_FIELDS = {
    "upload": "max_user_daily_upload_files",
    "ocr": "max_user_daily_ocr",
    "llm": "max_user_daily_llm",
}


class ResourceLimitError(ValueError):
    """A user-level storage or daily processing limit was reached."""


class ProcessingCapacityError(RuntimeError):
    """The bounded processing queue stayed full for too long."""


_processing_lock = threading.Lock()
_global_gate: tuple[int, threading.BoundedSemaphore] | None = None
_user_gates: dict[str, tuple[int, threading.BoundedSemaphore]] = {}


@contextmanager
def processing_slot(user_id: str | None, timeout_seconds: float = 300.0) -> Iterator[None]:
    """Bound concurrent invoice work in the configured single app process."""
    settings = get_settings()
    global _global_gate
    with _processing_lock:
        global_limit = int(settings.max_concurrent_processing_jobs)
        if _global_gate is None or _global_gate[0] != global_limit:
            _global_gate = (global_limit, threading.BoundedSemaphore(global_limit))
        global_gate = _global_gate[1]
        user_gate = None
        if user_id:
            user_limit = int(settings.max_concurrent_jobs_per_user)
            existing = _user_gates.get(user_id)
            if existing is None or existing[0] != user_limit:
                existing = (user_limit, threading.BoundedSemaphore(user_limit))
                if len(_user_gates) >= 10_000:
                    _user_gates.pop(next(iter(_user_gates)))
                _user_gates[user_id] = existing
            user_gate = existing[1]

    user_acquired = user_gate.acquire(timeout=timeout_seconds) if user_gate else True
    if not user_acquired:
        raise ProcessingCapacityError("个人处理队列繁忙，请稍后重新处理")
    global_acquired = False
    try:
        global_acquired = global_gate.acquire(timeout=timeout_seconds)
        if not global_acquired:
            raise ProcessingCapacityError("系统处理队列繁忙，请稍后重新处理")
        yield
    finally:
        if global_acquired:
            global_gate.release()
        if user_gate and user_acquired:
            user_gate.release()


def _today() -> str:
    settings = get_settings()
    try:
        zone = ZoneInfo(settings.tz)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.now(zone).strftime("%Y-%m-%d")


def get_tax_verify_daily_limit(db: Session) -> int:
    raw = get_value(db, TAX_VERIFY_DAILY_LIMIT_KEY, str(DEFAULT_TAX_VERIFY_DAILY_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TAX_VERIFY_DAILY_LIMIT
    if not MIN_TAX_VERIFY_DAILY_LIMIT <= value <= MAX_TAX_VERIFY_DAILY_LIMIT:
        return DEFAULT_TAX_VERIFY_DAILY_LIMIT
    return value


def set_tax_verify_daily_limit(db: Session, value: int | str) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("每日税务验票上限必须是整数") from exc
    if not MIN_TAX_VERIFY_DAILY_LIMIT <= limit <= MAX_TAX_VERIFY_DAILY_LIMIT:
        raise ValueError(
            f"每日税务验票上限必须在 {MIN_TAX_VERIFY_DAILY_LIMIT} 到 "
            f"{MAX_TAX_VERIFY_DAILY_LIMIT} 次之间"
        )
    set_value(db, TAX_VERIFY_DAILY_LIMIT_KEY, str(limit))
    db.commit()
    return limit


def get_tax_verify_usage(
    db: Session,
    user_id: str,
    usage_date: str | None = None,
) -> int:
    return int(
        db.scalar(
            select(TaxVerificationUsage.count).where(
                TaxVerificationUsage.user_id == user_id,
                TaxVerificationUsage.usage_date == (usage_date or _today()),
            )
        )
        or 0
    )


def reserve_tax_verification(
    db: Session,
    user_id: str | None,
    usage_date: str | None = None,
) -> tuple[bool, int, int]:
    """Atomically reserve one real provider call.

    Returns ``(allowed, used, limit)``. Cache hits never call this function.
    Legacy invoices without an owner are not subject to a per-user quota.
    """
    limit = get_tax_verify_daily_limit(db)
    if not user_id:
        return True, 0, limit

    day = usage_date or _today()
    statement = sqlite_insert(TaxVerificationUsage).values(
        user_id=user_id,
        usage_date=day,
        count=1,
        updated_at=utcnow(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=["user_id", "usage_date"],
        set_={
            "count": TaxVerificationUsage.count + 1,
            "updated_at": utcnow(),
        },
        where=TaxVerificationUsage.count < limit,
    ).returning(TaxVerificationUsage.count)
    used = db.scalar(statement)
    db.commit()
    if used is None:
        return False, get_tax_verify_usage(db, user_id, day), limit
    return True, int(used), limit


def get_resource_usage(
    db: Session,
    user_id: str,
    resource: str,
    usage_date: str | None = None,
) -> int:
    if resource not in RESOURCE_LIMIT_FIELDS:
        raise ValueError(f"未知资源类型：{resource}")
    return int(
        db.scalar(
            select(ResourceUsage.count).where(
                ResourceUsage.user_id == user_id,
                ResourceUsage.usage_date == (usage_date or _today()),
                ResourceUsage.resource == resource,
            )
        )
        or 0
    )


def resource_limit(resource: str) -> int:
    field = RESOURCE_LIMIT_FIELDS.get(resource)
    if not field:
        raise ValueError(f"未知资源类型：{resource}")
    return int(getattr(get_settings(), field))


def reserve_resource_usage(
    db: Session,
    user_id: str | None,
    resource: str,
    usage_date: str | None = None,
) -> tuple[bool, int, int]:
    """Atomically reserve one daily upload/OCR/LLM operation."""
    limit = resource_limit(resource)
    if not user_id:
        return True, 0, limit
    day = usage_date or _today()
    statement = sqlite_insert(ResourceUsage).values(
        user_id=user_id,
        usage_date=day,
        resource=resource,
        count=1,
        updated_at=utcnow(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=["user_id", "usage_date", "resource"],
        set_={"count": ResourceUsage.count + 1, "updated_at": utcnow()},
        where=ResourceUsage.count < limit,
    ).returning(ResourceUsage.count)
    used = db.scalar(statement)
    db.commit()
    if used is None:
        return False, get_resource_usage(db, user_id, resource, day), limit
    return True, int(used), limit


def release_resource_usage(
    db: Session,
    user_id: str | None,
    resource: str,
    usage_date: str | None = None,
) -> None:
    """Release a reservation when the guarded operation never started."""
    if not user_id:
        return
    row = db.scalar(
        select(ResourceUsage).where(
            ResourceUsage.user_id == user_id,
            ResourceUsage.usage_date == (usage_date or _today()),
            ResourceUsage.resource == resource,
        )
    )
    if row:
        row.count = max(0, row.count - 1)
        db.commit()


def assert_user_storage_available(db: Session, user_id: str | None, incoming_bytes: int) -> None:
    if not user_id:
        return
    limit_bytes = int(get_settings().max_user_storage_mb) * 1024 * 1024
    used = int(
        db.scalar(
            select(func.coalesce(func.sum(Invoice.file_size), 0)).where(
                Invoice.owner_id == user_id
            )
        )
        or 0
    )
    if incoming_bytes < 0 or used + incoming_bytes > limit_bytes:
        raise ResourceLimitError(
            f"个人存储空间已达到 {get_settings().max_user_storage_mb} MB 上限"
        )
