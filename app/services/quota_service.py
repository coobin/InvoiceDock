from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TaxVerificationUsage, utcnow
from app.services.settings_service import get_value, set_value

TAX_VERIFY_DAILY_LIMIT_KEY = "tax_verify_daily_limit"
DEFAULT_TAX_VERIFY_DAILY_LIMIT = 50
MIN_TAX_VERIFY_DAILY_LIMIT = 1
MAX_TAX_VERIFY_DAILY_LIMIT = 10000


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
