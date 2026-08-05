from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditLog, User

password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().app_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get("csrf_token", "")
    if not submitted or not expected or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="表单已过期，请刷新页面重试")


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if not user or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


def require_admin(request: Request, db: Session) -> User:
    user = require_user(request, db)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def bootstrap_admin(db: Session) -> User | None:
    settings = get_settings()
    if db.scalar(select(User.id).limit(1)):
        return None
    user = User(
        username=settings.admin_username,
        email=settings.admin_email,
        display_name="系统管理员",
        password_hash=hash_password(settings.admin_password),
        role="admin",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def record_audit(
    db: Session,
    request: Request | None,
    user: User | None,
    action: str,
    entity_type: str = "",
    entity_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    forwarded = request.headers.get("x-forwarded-for", "") if request else ""
    client_ip = forwarded.split(",")[0].strip() if forwarded else ""
    if not client_ip and request and request.client:
        client_ip = request.client.host
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            ip_address=client_ip,
        )
    )
    db.commit()


def mark_login(user: User, db: Session) -> None:
    user.last_login_at = datetime.now(UTC).replace(tzinfo=None)
    db.commit()

