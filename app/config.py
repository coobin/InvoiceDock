from __future__ import annotations

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "InvoiceDock · 票舱"
    app_secret: str = "development-only-change-me"
    app_base_url: str = "http://localhost:8765"
    data_dir: Path = Path("./data")
    database_url: str = "sqlite:///./data/invoicedock.db"
    session_https_only: bool = False
    enable_api_docs: bool = False
    # Only requests received directly from these proxy IPs/networks may supply
    # X-Forwarded-For. Keep empty when the application is exposed directly.
    trusted_proxy_ips: str = ""

    admin_username: str = "kay"
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

    # 邮箱 + 密码自助注册。部署方可在 .env 中关闭（false）后仅保留
    # 管理员/邀请制账户。注册账户默认为普通成员，密码使用 Argon2 哈希。
    registration_enabled: bool = True
    registration_min_password_length: int = 12
    password_max_length: int = 256
    # Comma-separated additions to the built-in reserved username list.
    reserved_usernames: str = ""

    max_upload_mb: int = 25
    max_user_storage_mb: int = 2048
    max_user_daily_upload_files: int = 200
    max_user_daily_ocr: int = 200
    max_user_daily_llm: int = 100
    max_concurrent_jobs_per_user: int = 2
    max_concurrent_processing_jobs: int = 4
    max_archive_files: int = 30
    max_archive_uncompressed_mb: int = 80
    max_ofd_files: int = 200
    max_ofd_uncompressed_mb: int = 100
    # Comma-separated hostnames/IPs that are deliberately allowed even when
    # they resolve to a private/link-local address.
    outbound_private_host_allowlist: str = ""
    mail_scan_interval_minutes: int = 10
    mail_fetch_limit: int = 100
    tz: str = "Asia/Shanghai"
    log_level: str = "INFO"

    # Optional integration overrides (税务 / LLM). When a value is set here
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

    # 查验方式开关（环境变量可覆盖）：发票云 / 本地 OCR / LLM 是否允许使用。
    # 关闭本地回退后，发票云查验失败的发票直接进入人工复核。
    verify_provider: str = "true"
    verify_ocr: str = "true"
    verify_llm: str = "true"

    # 税务发票云 · 标准版（Piaozone）override。使用 client_id/client_secret
    # 签名授权（/base/oauth/token），识别查验走 /m3/bill/invoice/img/Check/info。
    piaozone_enabled: str = ""
    piaozone_base_url: str = "https://api.piaozone.com"
    piaozone_client_id: str = ""
    piaozone_client_secret: str = ""
    piaozone_encrypt_key: str = ""
    piaozone_sign_method: str = "MD5"
    piaozone_token_path: str = "/base/oauth/token"
    piaozone_invoice_check_path: str = "/m3/bill/invoice/img/Check/info"

    # 收票抬头预设（JSON 数组）。每项支持 name/tax_id/address/phone/
    # bank_name/bank_account/bank_code。留空表示不限制抬头；
    # 配置后仅接受预设抬头、用户自行新增抬头或包含本人姓名的抬头。
    invoice_titles_json: str = ""

    @field_validator("app_base_url", "oidc_issuer")
    @classmethod
    def trim_urls(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator(
        "max_upload_mb",
        "max_user_storage_mb",
        "max_user_daily_upload_files",
        "max_user_daily_ocr",
        "max_user_daily_llm",
        "max_concurrent_jobs_per_user",
        "max_concurrent_processing_jobs",
        "max_archive_files",
        "max_archive_uncompressed_mb",
        "max_ofd_files",
        "max_ofd_uncompressed_mb",
    )
    @classmethod
    def validate_resource_limits(cls, value: int, info: ValidationInfo) -> int:
        upper_bounds = {
            "max_upload_mb": 1024,
            "max_user_storage_mb": 1_048_576,
            "max_user_daily_upload_files": 1_000_000,
            "max_user_daily_ocr": 1_000_000,
            "max_user_daily_llm": 1_000_000,
            "max_concurrent_jobs_per_user": 128,
            "max_concurrent_processing_jobs": 512,
            "max_archive_files": 10_000,
            "max_archive_uncompressed_mb": 102_400,
            "max_ofd_files": 10_000,
            "max_ofd_uncompressed_mb": 102_400,
        }
        maximum = upper_bounds[info.field_name]
        if not 1 <= value <= maximum:
            raise ValueError(f"{info.field_name} must be between 1 and {maximum}")
        return value

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

    @property
    def additional_reserved_usernames(self) -> set[str]:
        return {item.strip() for item in self.reserved_usernames.split(",") if item.strip()}

    @property
    def trusted_proxy_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        networks: list[IPv4Network | IPv6Network] = []
        for item in self.trusted_proxy_ips.split(","):
            value = item.strip()
            if not value:
                continue
            try:
                networks.append(ip_network(value, strict=False))
            except ValueError:
                # Invalid entries are deliberately ignored instead of making
                # the service trust an unexpectedly broad source.
                continue
        return tuple(networks)


@lru_cache
def get_settings() -> Settings:
    return Settings()
