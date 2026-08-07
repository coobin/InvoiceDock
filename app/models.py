from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="member")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("sha256", name="uq_invoice_sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(255), unique=True)
    mime_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(30), default="upload", index=True)
    source_ref: Mapped[str] = mapped_column(String(500), default="")
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    verification_method: Mapped[str] = mapped_column(String(30), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str] = mapped_column(Text, default="")

    invoice_type: Mapped[str] = mapped_column(String(80), default="")
    invoice_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    invoice_number: Mapped[str] = mapped_column(String(60), default="", index=True)
    invoice_date: Mapped[str] = mapped_column(String(20), default="", index=True)
    check_code: Mapped[str] = mapped_column(String(40), default="")
    seller_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    seller_tax_id: Mapped[str] = mapped_column(String(40), default="")
    buyer_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    buyer_tax_id: Mapped[str] = mapped_column(String(40), default="")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(10), default="CNY")
    category: Mapped[str] = mapped_column(String(80), default="未分类", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    title_warning: Mapped[str] = mapped_column(Text, default="")

    raw_text: Mapped[str] = mapped_column(Text, default="")
    ocr_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    kingdee_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    conflicts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    duplicate_of: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Mailbox(Base):
    __tablename__ = "mailboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(120))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=993)
    username: Mapped[str] = mapped_column(String(255))
    password_encrypted: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(String(255), default="INBOX")
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"
    __table_args__ = (UniqueConstraint("mailbox_id", "uid", name="uq_mailbox_uid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True)
    uid: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    sender: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(30), default="processed")
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserIntegration(Base):
    """Per-user integration overrides. Admin-level values live in AppSetting;
    a user's own values fall back to admin settings when absent."""

    __tablename__ = "user_integrations"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_integration_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserTitle(Base):
    """用户自行新增的收票抬头（叠加在管理员 env 预设之上）。"""

    __tablename__ = "user_titles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    tax_id: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(500), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    bank_name: Mapped[str] = mapped_column(String(255), default="")
    bank_account: Mapped[str] = mapped_column(String(100), default="")
    bank_code: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthState(Base):
    """服务端 OIDC state 存储。把登录临时状态放数据库而不是会话 cookie，
    避免浏览器跨站回跳时丢弃 cookie 导致 mismatching_state。"""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(80), default="")
    entity_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(20), default="info", index=True)
    event: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class VerificationCache(Base):
    """当天已通过发票云查验的发票号本地缓存。

    同一发票号在当天再次上传时直接复用查验结果，不再调用税务发票云，
    避免消耗单张发票每日查验次数。每个 (发票号, 日期) 只保留一条记录。
    """

    __tablename__ = "verification_caches"
    __table_args__ = (
        UniqueConstraint("invoice_number", "verify_date", name="uq_verify_cache_number_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_number: Mapped[str] = mapped_column(String(60), index=True)
    verify_date: Mapped[str] = mapped_column(String(10), index=True)
    method: Mapped[str] = mapped_column(String(30), default="")
    fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    kingdee_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
