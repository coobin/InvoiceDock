from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AppSetting
from app.security import decrypt_secret, encrypt_secret

SECRET_KEYS = {
    "kingdee_app_secret",
    "kingdee_access_token",
    "llm_api_key",
}


INTEGRATION_DEFAULTS = {
    "kingdee_enabled": "false",
    "kingdee_base_url": "",
    "kingdee_app_id": "",
    "kingdee_app_secret": "",
    "kingdee_account_id": "",
    "kingdee_tenant_id": "1",
    "kingdee_user": "",
    "kingdee_org_number": "",
    "kingdee_tax_no": "",
    "kingdee_company_name": "",
    "llm_enabled": "false",
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4.1-mini",
    "llm_vision": "true",
}


def get_value(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    if not row:
        return default
    return decrypt_secret(row.value) if row.encrypted else row.value


def set_value(db: Session, key: str, value: str, secret: bool | None = None) -> None:
    encrypted = key in SECRET_KEYS if secret is None else secret
    stored = encrypt_secret(value) if encrypted and value else value
    row = db.get(AppSetting, key)
    if row:
        row.value = stored
        row.encrypted = encrypted
    else:
        db.add(AppSetting(key=key, value=stored, encrypted=encrypted))


def get_integrations(db: Session, mask_secrets: bool = False) -> dict[str, str]:
    values = {key: get_value(db, key, default) for key, default in INTEGRATION_DEFAULTS.items()}
    if mask_secrets:
        for key in SECRET_KEYS:
            values[key] = "••••••••" if values.get(key) else ""
    return values


def update_integrations(db: Session, values: dict[str, str]) -> None:
    for key in INTEGRATION_DEFAULTS:
        if key not in values:
            continue
        value = values[key].strip()
        if key in SECRET_KEYS and value == "••••••••":
            continue
        set_value(db, key, value)
    db.commit()


def as_bool(value: str | bool | None) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}

