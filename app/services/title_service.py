from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, UserTitle

TITLE_FIELDS = ("name", "tax_id", "address", "phone", "bank_name", "bank_account", "bank_code")


def parse_presets(raw: str) -> list[dict[str, str]]:
    """Parse the INVOICE_TITLES_JSON env value into preset title entries."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    presets: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        entry = {field: str(item.get(field, "")).strip() for field in TITLE_FIELDS}
        if entry["name"] or entry["tax_id"]:
            presets.append(entry)
    return presets


def env_presets() -> list[dict[str, str]]:
    return parse_presets(get_settings().invoice_titles_json)


def user_titles(db: Session, user_id: str) -> list[UserTitle]:
    return list(
        db.scalars(
            select(UserTitle).where(UserTitle.user_id == user_id).order_by(UserTitle.created_at.desc())
        ).all()
    )


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _matches(preset: dict[str, str], buyer_name: str, buyer_tax_id: str) -> bool:
    name, tax = _norm(preset.get("name", "")), _norm(preset.get("tax_id", ""))
    buyer, buyer_tax = _norm(buyer_name), _norm(buyer_tax_id)
    if name and buyer and (name == buyer or name in buyer or buyer in name):
        return True
    if tax and buyer_tax and tax == buyer_tax:
        return True
    return False


def _user_name_matches(user: User | None, buyer_name: str) -> bool:
    if not user or not buyer_name:
        return False
    buyer = _norm(buyer_name)
    for candidate in (user.display_name, user.username):
        value = _norm(candidate)
        if len(value) >= 2 and value in buyer:
            return True
    return False


def title_warning(db: Session, user: User | None, buyer_name: str, buyer_tax_id: str) -> str:
    """Return a warning message when the invoice title is not allowed, else ''.

    Rules: no presets at all -> accept everything. Otherwise only accept
    admin env presets, the user's own added titles, or titles containing the
    owner's display name / username.
    """
    presets = env_presets()
    custom = user_titles(db, user.id) if user else []
    if not presets and not custom:
        return ""
    if not buyer_name and not buyer_tax_id:
        return ""
    for preset in presets:
        if _matches(preset, buyer_name, buyer_tax_id):
            return ""
    for row in custom:
        preset = {field: str(getattr(row, field, "") or "") for field in TITLE_FIELDS}
        if _matches(preset, buyer_name, buyer_tax_id):
            return ""
    if _user_name_matches(user, buyer_name):
        return ""
    display = buyer_name or f"税号 {buyer_tax_id}"
    return f"抬头未匹配预设：{display}，请确认是否为本公司或本人票据"
