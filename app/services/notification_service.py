from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import JobLog
from app.services.settings_service import as_bool, get_value, set_value

BARK_URL_KEY = "bark_url"
BARK_ENABLED_KEY = "bark_enabled"
BARK_EVENT_KEYS = {
    "register": "bark_notify_register",
    "login": "bark_notify_login",
    "usage": "bark_notify_usage",
}

MASKED_SECRET = "••••••••"
REQUEST_TIMEOUT_SECONDS = 8.0

_EVENT_ALIASES = {
    "registration": "register",
    "signup": "register",
    "sign_in": "login",
    "signin": "login",
    "use": "usage",
}


class NotificationConfigurationError(ValueError):
    """The Bark notification configuration is missing or invalid."""


class NotificationDeliveryError(RuntimeError):
    """Bark accepted the HTTP request but reported a delivery failure."""


def _normalise_event_type(event_type: str) -> str:
    value = event_type.strip().lower()
    return _EVENT_ALIASES.get(value, value)


def _validate_bark_url(value: str) -> str:
    url = value.strip()
    if len(url) > 2000:
        raise NotificationConfigurationError("Bark 推送地址过长")
    try:
        parsed = urlsplit(url)
        # Accessing port also validates malformed values such as :not-a-port.
        _ = parsed.port
    except ValueError as exc:
        raise NotificationConfigurationError("Bark 推送地址格式不正确") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise NotificationConfigurationError("Bark 推送地址只支持 http 或 https")
    if not parsed.hostname:
        raise NotificationConfigurationError("Bark 推送地址缺少服务器域名")
    if parsed.username is not None or parsed.password is not None:
        raise NotificationConfigurationError("Bark 推送地址不能包含用户名或密码")
    if not parsed.path.strip("/"):
        raise NotificationConfigurationError("Bark 推送地址缺少设备密钥")
    if parsed.fragment:
        raise NotificationConfigurationError("Bark 推送地址不能包含片段")
    return url


def mask_bark_url(value: str) -> str:
    """Hide the device key (the final URL path component) for display."""
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return MASKED_SECRET

    path_parts = parsed.path.split("/")
    key_index = next(
        (index for index in range(len(path_parts) - 1, -1, -1) if path_parts[index]),
        None,
    )
    if key_index is None:
        return MASKED_SECRET
    path_parts[key_index] = MASKED_SECRET
    masked = SplitResult(
        parsed.scheme,
        parsed.netloc,
        "/".join(path_parts),
        parsed.query,
        parsed.fragment,
    )
    return urlunsplit(masked)


def get_notification_settings(
    db: Session,
    mask_secret: bool = False,
) -> dict[str, str | bool]:
    """Return the global Bark settings in a form suitable for the admin UI."""
    bark_url = get_value(db, BARK_URL_KEY, "")
    return {
        "bark_url": mask_bark_url(bark_url) if mask_secret else bark_url,
        "enabled": as_bool(get_value(db, BARK_ENABLED_KEY, "false")),
        "register": as_bool(get_value(db, BARK_EVENT_KEYS["register"], "false")),
        "login": as_bool(get_value(db, BARK_EVENT_KEYS["login"], "false")),
        "usage": as_bool(get_value(db, BARK_EVENT_KEYS["usage"], "false")),
    }


def save_notification_settings(
    db: Session,
    values: Mapping[str, Any] | None = None,
    *,
    bark_url: str | None = None,
    enabled: str | bool | None = None,
    register: str | bool | None = None,
    login: str | bool | None = None,
    usage: str | bool | None = None,
) -> dict[str, str | bool]:
    """Validate and persist global Bark settings.

    A mapping can be supplied by form handlers, or callers can use keyword
    arguments.  Supplying the masked URL keeps the existing encrypted value.
    """
    supplied = dict(values or {})
    arguments = {
        "bark_url": bark_url,
        "enabled": enabled,
        "register": register,
        "login": login,
        "usage": usage,
    }
    for key, value in arguments.items():
        if value is not None:
            supplied[key] = value

    current = get_notification_settings(db)
    submitted_url = supplied.get("bark_url")
    if submitted_url is None or MASKED_SECRET in str(submitted_url):
        final_url = str(current["bark_url"])
    else:
        final_url = str(submitted_url).strip()
        if final_url:
            final_url = _validate_bark_url(final_url)

    final_enabled = as_bool(supplied.get("enabled", current["enabled"]))
    if final_enabled and not final_url:
        raise NotificationConfigurationError("启用 Bark 推送前请填写推送地址")

    final_values = {
        "enabled": final_enabled,
        "register": as_bool(supplied.get("register", current["register"])),
        "login": as_bool(supplied.get("login", current["login"])),
        "usage": as_bool(supplied.get("usage", current["usage"])),
    }

    # The URL contains the Bark device key and must never be stored as plaintext.
    set_value(db, BARK_URL_KEY, final_url, secret=True)
    set_value(db, BARK_ENABLED_KEY, str(final_values["enabled"]).lower())
    for event_type, setting_key in BARK_EVENT_KEYS.items():
        set_value(db, setting_key, str(final_values[event_type]).lower())
    db.commit()
    return {"bark_url": final_url, **final_values}


def send_bark_notification(url: str, title: str, body: str) -> None:
    """Send one Bark message, raising on invalid configuration or delivery failure."""
    target = _validate_bark_url(url)
    payload = {"title": str(title), "body": str(body), "group": "InvoiceDock"}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.post(target, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise NotificationDeliveryError(
            f"Bark 服务返回 HTTP {exc.response.status_code}"
        ) from None
    except httpx.RequestError:
        raise NotificationDeliveryError("无法连接 Bark 服务") from None

    # Official Bark and most compatible servers return {"code": 200}.  Keep
    # accepting non-JSON 2xx responses for lightweight self-hosted gateways.
    try:
        result = response.json()
    except ValueError:
        return
    if isinstance(result, dict) and "code" in result:
        try:
            success = int(result["code"]) == 200
        except (TypeError, ValueError):
            success = False
        if not success:
            message = str(result.get("message") or result.get("error") or "Bark 推送失败")
            raise NotificationDeliveryError(message[:500])


def test_bark_notification(
    db: Session,
    title: str = "InvoiceDock 测试通知",
    body: str = "Bark 消息推送配置成功",
) -> None:
    """Strict admin test: deliberately let configuration/network errors propagate."""
    settings = get_notification_settings(db)
    bark_url = str(settings["bark_url"])
    if not bark_url:
        raise NotificationConfigurationError("请先保存 Bark 推送地址")
    send_bark_notification(bark_url, title, body)


def _record_failure(
    db: Session,
    event_type: str,
    exc: Exception,
    details: Mapping[str, Any] | None,
) -> None:
    log_details = dict(details or {})
    log_details["notification_type"] = event_type
    db.add(
        JobLog(
            level="error",
            event="notification.failed",
            message=f"Bark {event_type} 推送失败：{exc}"[:1000],
            details=log_details,
        )
    )
    db.commit()


def notify_event(
    db: Session,
    event_type: str,
    title: str,
    body: str,
    details: Mapping[str, Any] | None = None,
) -> bool:
    """Best-effort business notification which never interrupts the main action."""
    normalised_type = _normalise_event_type(event_type)
    try:
        if normalised_type not in BARK_EVENT_KEYS:
            raise NotificationConfigurationError(f"不支持的消息类型：{event_type}")
        settings = get_notification_settings(db)
        if not settings["enabled"] or not settings[normalised_type]:
            return False
        send_bark_notification(str(settings["bark_url"]), title, body)
        return True
    except Exception as exc:  # A notification must never break registration/login/usage.
        try:
            _record_failure(db, normalised_type, exc, details)
        except Exception:
            # Database/logging failures are also non-fatal to the business action.
            try:
                db.rollback()
            except Exception:
                pass
        return False


def notify_event_background(
    event_type: str,
    title: str,
    body: str,
    details: Mapping[str, Any] | None = None,
) -> bool:
    """BackgroundTasks-compatible wrapper with its own short-lived DB session."""
    db = SessionLocal()
    try:
        return notify_event(db, event_type, title, body, details)
    finally:
        db.close()
