from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting, UserIntegration
from app.security import decrypt_secret, encrypt_secret

SECRET_KEYS = {
    "kingdee_app_secret",
    "kingdee_access_token",
    "llm_api_key",
    "piaozone_client_secret",
    "piaozone_encrypt_key",
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
    "piaozone_enabled": "false",
    "piaozone_base_url": "https://api.piaozone.com",
    "piaozone_client_id": "",
    "piaozone_client_secret": "",
    "piaozone_encrypt_key": "",
    "piaozone_sign_method": "MD5",
    "piaozone_token_path": "/base/oauth/token",
    "piaozone_invoice_check_path": "/m3/bill/invoice/img/Check/info",
    "verify_provider": "true",
    "verify_ocr": "true",
    "verify_llm": "true",
}

KINGDEE_KEYS = frozenset(key for key in INTEGRATION_DEFAULTS if key.startswith("kingdee_"))
PIAOZONE_KEYS = frozenset(key for key in INTEGRATION_DEFAULTS if key.startswith("piaozone_"))
LLM_KEYS = frozenset(key for key in INTEGRATION_DEFAULTS if key.startswith("llm_"))
INTEGRATION_KEYS = {
    "kingdee": KINGDEE_KEYS,
    "piaozone": PIAOZONE_KEYS,
    "llm": LLM_KEYS,
}
# 管理员可在“查验集成”页开关的 OIDC 登录总闸。OIDC 客户端参数仍由
# 环境变量提供（避免密钥入库）；此开关只控制登录入口与自动跳转是否生效。
OIDC_TOGGLE_KEY = "oidc_enabled"
USER_TAX_VERIFY_KEY = "tax_verify_enabled"
USER_CONFIGURABLE_INTEGRATIONS = ("llm",)


def env_values() -> dict[str, str]:
    """Values provided via environment / .env. Non-empty values take
    precedence over database rows and are never written to the database."""
    settings = get_settings()
    return {
        key: str(getattr(settings, key))
        for key in INTEGRATION_DEFAULTS
        if getattr(settings, key, "") not in (None, "")
    }


def get_env_keys() -> set[str]:
    return set(env_values())


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


def _user_rows(db: Session, user_id: str) -> dict[str, str]:
    rows = db.scalars(select(UserIntegration).where(UserIntegration.user_id == user_id)).all()
    return {
        row.key: decrypt_secret(row.value) if row.encrypted else row.value
        for row in rows
    }


def user_custom_integrations(db: Session, user_id: str) -> set[str]:
    """Integrations for which the user has saved their own configuration."""
    rows = _user_rows(db, user_id)
    return {
        integration
        for integration in USER_CONFIGURABLE_INTEGRATIONS
        if f"{integration}_enabled" in rows
    }


def get_user_tax_verify_enabled(db: Session, user_id: str) -> bool:
    return as_bool(_user_rows(db, user_id).get(USER_TAX_VERIFY_KEY, "true"))


def set_user_tax_verify_enabled(db: Session, user_id: str, enabled: bool) -> None:
    row = db.scalar(
        select(UserIntegration).where(
            UserIntegration.user_id == user_id,
            UserIntegration.key == USER_TAX_VERIFY_KEY,
        )
    )
    value = "true" if enabled else "false"
    if row:
        row.value = value
        row.encrypted = False
    else:
        db.add(
            UserIntegration(
                user_id=user_id,
                key=USER_TAX_VERIFY_KEY,
                value=value,
                encrypted=False,
            )
        )
    db.commit()


def get_integrations(
    db: Session,
    user_id: str | None = None,
    mask_secrets: bool = False,
) -> dict[str, str]:
    """Effective integration config for a user.

    Precedence: environment > admin (global) database rows > defaults;
    a user's own saved configuration overrides everything above it.
    """
    values = dict(INTEGRATION_DEFAULTS)
    values.update(env_values())
    for key in INTEGRATION_DEFAULTS:
        values[key] = get_value(db, key, values[key])
    if user_id:
        user_vals = _user_rows(db, user_id)
        for integration in USER_CONFIGURABLE_INTEGRATIONS:
            if f"{integration}_enabled" not in user_vals:
                continue
            for key in INTEGRATION_KEYS[integration]:
                if key in user_vals:
                    values[key] = user_vals[key]
        provider_enabled = as_bool(values.get("verify_provider", "true"))
        user_provider_enabled = as_bool(user_vals.get(USER_TAX_VERIFY_KEY, "true"))
        values["verify_provider"] = "true" if provider_enabled and user_provider_enabled else "false"
    if mask_secrets:
        for key in SECRET_KEYS:
            values[key] = "••••••••" if values.get(key) else ""
    return values


def update_integrations(
    db: Session,
    values: dict[str, str],
    user_id: str | None = None,
) -> None:
    if user_id:
        for key in INTEGRATION_DEFAULTS:
            if key not in values:
                continue
            value = values[key].strip()
            if key in SECRET_KEYS and value == "••••••••":
                continue
            encrypted = key in SECRET_KEYS
            stored = encrypt_secret(value) if encrypted and value else value
            row = db.scalar(
                select(UserIntegration).where(
                    UserIntegration.user_id == user_id, UserIntegration.key == key
                )
            )
            if row:
                row.value = stored
                row.encrypted = encrypted
            else:
                db.add(
                    UserIntegration(
                        user_id=user_id, key=key, value=stored, encrypted=encrypted
                    )
                )
        db.commit()
        return
    for key in INTEGRATION_DEFAULTS:
        if key not in values:
            continue
        value = values[key].strip()
        if key in SECRET_KEYS and value == "••••••••":
            continue
        set_value(db, key, value)
    db.commit()


def clear_user_integration(db: Session, user_id: str, integration: str) -> None:
    keys = INTEGRATION_KEYS[integration]
    rows = db.scalars(
        select(UserIntegration).where(
            UserIntegration.user_id == user_id, UserIntegration.key.in_(keys)
        )
    ).all()
    for row in rows:
        db.delete(row)
    db.commit()


def as_bool(value: str | bool | None) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def oidc_enabled(db: Session) -> bool:
    """OIDC 是否实际可用：环境变量已配置 OIDC 且管理员未在界面关闭。"""
    settings = get_settings()
    if not (settings.oidc_enabled and settings.oidc_issuer and settings.oidc_client_id):
        return False
    default = "true" if settings.oidc_enabled else "false"
    return as_bool(get_value(db, OIDC_TOGGLE_KEY, default))
