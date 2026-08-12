from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from time import monotonic
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


@dataclass
class _ThrottleBucket:
    stamps: list[float]
    expires_at: float


_throttle: OrderedDict[str, _ThrottleBucket] = OrderedDict()
_throttle_lock = threading.Lock()
_THROTTLE_MAX_BUCKETS = 10_000
_THROTTLE_CLEANUP_INTERVAL = 60.0
_throttle_next_cleanup = 0.0

DEFAULT_RESERVED_USERNAMES = frozenset(
    {
        "abuse",
        "account",
        "admin",
        "administrator",
        "billing",
        "help",
        "helpdesk",
        "invoice",
        "invoicedock",
        "kay",
        "moderator",
        "no-reply",
        "noreply",
        "operator",
        "owner",
        "postmaster",
        "root",
        "security",
        "service",
        "staff",
        "support",
        "sysadmin",
        "system",
        "superuser",
        "webmaster",
        "官方",
        "客服",
        "平台",
        "管理员",
        "系统管理员",
    }
)


def _normalise_reserved_username(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    if "@" in text:
        text = text.split("@", 1)[0]
    text = text.split("+", 1)[0]
    return re.sub(r"[\s._-]+", "", text)


def reserved_usernames() -> set[str]:
    settings = get_settings()
    configured = {
        *DEFAULT_RESERVED_USERNAMES,
        *settings.additional_reserved_usernames,
        settings.admin_username,
    }
    return {_normalise_reserved_username(item) for item in configured if item}


def is_reserved_username(value: str) -> bool:
    return _normalise_reserved_username(value) in reserved_usernames()


def client_ip(request: Request) -> str:
    """Return the originating IP without trusting user-supplied forwarding.

    ``X-Forwarded-For`` is considered only when the immediate network peer is
    in ``TRUSTED_PROXY_IPS``. The chain is then walked from right to left so a
    trusted proxy cannot be tricked by a client-prepended value.
    """
    peer = request.client.host.strip() if request.client and request.client.host else ""
    try:
        peer_address = ip_address(peer)
    except ValueError:
        return peer or "unknown"

    trusted = get_settings().trusted_proxy_networks
    if not any(peer_address in network for network in trusted):
        return str(peer_address)

    forwarded: list[str] = []
    for value in request.headers.get("x-forwarded-for", "").split(","):
        try:
            forwarded.append(str(ip_address(value.strip())))
        except ValueError:
            continue
    if not forwarded:
        return str(peer_address)

    for value in reversed(forwarded):
        address = ip_address(value)
        if not any(address in network for network in trusted):
            return value
    return forwarded[0]


def throttle_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Return True when the key has exceeded `limit` calls in the window.

    Simple in-memory sliding window, sufficient for a single-process
    uvicorn deployment; restart resets counters (acceptable tradeoff).
    """
    if limit < 1 or window_seconds < 1:
        raise ValueError("limit and window_seconds must be positive")
    now = monotonic()
    global _throttle_next_cleanup
    with _throttle_lock:
        if now >= _throttle_next_cleanup:
            expired = [name for name, bucket in _throttle.items() if bucket.expires_at <= now]
            for name in expired:
                _throttle.pop(name, None)
            _throttle_next_cleanup = now + _THROTTLE_CLEANUP_INTERVAL

        existing = _throttle.get(key)
        stamps = (
            [stamp for stamp in existing.stamps if now - stamp < window_seconds]
            if existing
            else []
        )
        if existing is None and len(_throttle) >= _THROTTLE_MAX_BUCKETS:
            _throttle.popitem(last=False)
        bucket = _ThrottleBucket(stamps=stamps, expires_at=now + window_seconds)
        _throttle[key] = bucket
        _throttle.move_to_end(key)
        if len(stamps) >= limit:
            return True
        stamps.append(now)
        return False


def throttle_reset(key: str) -> None:
    with _throttle_lock:
        _throttle.pop(key, None)


def password_policy_error(password: str, minimum_length: int = 12) -> str | None:
    """Return a user-facing password policy error, or ``None`` when valid."""
    if len(password) < max(12, minimum_length):
        return f"密码至少需要 {max(12, minimum_length)} 个字符"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "密码需要同时包含字母和数字"
    return None


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
    user = db.get(User, user_id)
    if not user:
        request.session.clear()
        return None
    supplied = str(request.session.get("auth_marker", ""))
    expected = session_auth_marker(user)
    if not supplied or not hmac.compare_digest(supplied, expected):
        request.session.clear()
        return None
    return user


def session_auth_marker(user: User) -> str:
    """Bind a signed session to security-sensitive account state.

    Password, OIDC identity, role and active-state changes invalidate every
    previously issued session cookie, including cookies copied to another
    browser before a password reset.
    """
    material = "\x1f".join(
        (
            user.id,
            user.password_hash or "",
            user.oidc_subject or "",
            user.role,
            "1" if user.active else "0",
            str(getattr(user, "session_version", 0)),
        )
    )
    return hmac.new(
        get_settings().app_secret.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def start_user_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["auth_marker"] = session_auth_marker(user)


def rotate_user_sessions(user: User) -> None:
    """Invalidate all existing sessions for an account."""
    if hasattr(user, "session_version"):
        user.session_version += 1


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
    source_ip = client_ip(request) if request else ""
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            ip_address=source_ip,
        )
    )
    db.commit()


def mark_login(user: User, db: Session) -> None:
    user.last_login_at = datetime.now(UTC).replace(tzinfo=None)
    db.commit()
