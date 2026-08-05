from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "InvoiceDock · 票舱"
    app_secret: str = "development-only-change-me"
    app_base_url: str = "http://localhost:8765"
    data_dir: Path = Path("./data")
    database_url: str = "sqlite:///./data/invoicedock.db"
    session_https_only: bool = False

    admin_username: str = "admin"
    admin_password: str = "change-me-now"
    admin_email: str = "admin@example.com"

    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    oidc_allowed_domains: str = ""
    oidc_admin_group: str = ""
    oidc_group_claim: str = "groups"

    max_upload_mb: int = 25
    mail_scan_interval_minutes: int = 10
    mail_fetch_limit: int = 100
    tz: str = "Asia/Shanghai"
    log_level: str = "INFO"

    # Optional integration overrides (金蝶 / LLM). When a value is set here
    # (via environment or .env), it takes precedence over database-stored
    # settings and is never persisted to the database.
    kingdee_enabled: str = ""
    kingdee_base_url: str = ""
    kingdee_app_id: str = ""
    kingdee_app_secret: str = ""
    kingdee_account_id: str = ""
    kingdee_tenant_id: str = ""
    kingdee_user: str = ""
    kingdee_org_number: str = ""
    kingdee_tax_no: str = ""
    kingdee_company_name: str = ""
    llm_enabled: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_vision: str = ""

    @field_validator("app_base_url", "oidc_issuer")
    @classmethod
    def trim_urls(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def oidc_domains(self) -> set[str]:
        return {item.strip().lower() for item in self.oidc_allowed_domains.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
