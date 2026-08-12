from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AppSetting, JobLog
from app.services import notification_service


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'notifications.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as db:
        yield db


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None):
        self.status_code = status_code
        self._payload = {"code": 200} if payload is None else payload
        self.request = httpx.Request("POST", "https://api.day.app/device-key")

    def raise_for_status(self) -> None:
        if self.status_code >= 300:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("failed", request=self.request, response=response)

    def json(self) -> Any:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse, captured: dict[str, Any], **kwargs: Any):
        self.response = response
        self.captured = captured
        self.captured["client_kwargs"] = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, str]) -> FakeResponse:
        self.captured["url"] = url
        self.captured["json"] = json
        return self.response


def _install_fake_client(
    monkeypatch,
    *,
    response: FakeResponse | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    selected_response = response or FakeResponse()

    def factory(**kwargs: Any) -> FakeClient:
        return FakeClient(selected_response, captured, **kwargs)

    monkeypatch.setattr(notification_service.httpx, "Client", factory)
    return captured


def _enable(db: Session, **overrides: Any) -> None:
    values = {
        "bark_url": "https://api.day.app/device-key",
        "enabled": True,
        "register": True,
        "login": True,
        "usage": True,
    }
    values.update(overrides)
    notification_service.save_notification_settings(db, values)


def test_settings_encrypt_url_and_mask_device_key(db_session):
    _enable(db_session)

    row = db_session.get(AppSetting, notification_service.BARK_URL_KEY)
    assert row is not None
    assert row.encrypted is True
    assert "device-key" not in row.value
    assert notification_service.get_notification_settings(db_session) == {
        "bark_url": "https://api.day.app/device-key",
        "enabled": True,
        "register": True,
        "login": True,
        "usage": True,
    }
    assert notification_service.get_notification_settings(db_session, mask_secret=True)[
        "bark_url"
    ] == f"https://api.day.app/{notification_service.MASKED_SECRET}"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.day.app/device-key",
        "javascript:alert(1)",
        "https://api.day.app",
        "https://user:password@api.day.app/device-key",
    ],
)
def test_save_rejects_non_http_or_incomplete_urls(db_session, url):
    with pytest.raises(notification_service.NotificationConfigurationError):
        notification_service.save_notification_settings(
            db_session,
            bark_url=url,
            enabled=True,
            register=True,
            login=False,
            usage=False,
        )


def test_notify_event_posts_expected_json(monkeypatch, db_session):
    _enable(db_session)
    captured = _install_fake_client(monkeypatch)

    assert notification_service.notify_event(
        db_session,
        "register",
        "新用户注册",
        "user@example.com",
    ) is True
    assert captured["url"] == "https://api.day.app/device-key"
    assert captured["json"] == {
        "title": "新用户注册",
        "body": "user@example.com",
        "group": "InvoiceDock",
    }
    assert captured["client_kwargs"] == {
        "timeout": notification_service.REQUEST_TIMEOUT_SECONDS,
        "follow_redirects": False,
    }


def test_notify_event_does_nothing_when_globally_disabled(monkeypatch, db_session):
    _enable(db_session, enabled=False)

    def unexpected_client(**_kwargs: Any):
        raise AssertionError("disabled notifications must not make an HTTP request")

    monkeypatch.setattr(notification_service.httpx, "Client", unexpected_client)
    assert notification_service.notify_event(db_session, "register", "title", "body") is False
    assert db_session.scalar(select(JobLog)) is None


def test_notify_event_respects_individual_event_switch(monkeypatch, db_session):
    _enable(db_session, login=False)

    def unexpected_client(**_kwargs: Any):
        raise AssertionError("disabled event type must not make an HTTP request")

    monkeypatch.setattr(notification_service.httpx, "Client", unexpected_client)
    assert notification_service.notify_event(db_session, "login", "title", "body") is False
    assert db_session.scalar(select(JobLog)) is None


def test_notify_event_swallows_failure_and_records_job_log(monkeypatch, db_session):
    _enable(db_session)
    _install_fake_client(monkeypatch, response=FakeResponse(status_code=500))

    assert notification_service.notify_event(
        db_session,
        "usage",
        "导出发票",
        "导出 4 张",
        {"user_id": "user-1"},
    ) is False

    log = db_session.scalar(select(JobLog).where(JobLog.event == "notification.failed"))
    assert log is not None
    assert log.level == "error"
    assert log.details == {"user_id": "user-1", "notification_type": "usage"}
    assert "Bark usage 推送失败" in log.message


def test_strict_test_propagates_delivery_failure(monkeypatch, db_session):
    _enable(db_session, enabled=False)
    _install_fake_client(
        monkeypatch,
        response=FakeResponse(payload={"code": 400, "message": "invalid key"}),
    )

    with pytest.raises(notification_service.NotificationDeliveryError, match="invalid key"):
        notification_service.test_bark_notification(db_session)


def test_background_wrapper_creates_and_closes_session(monkeypatch):
    calls: dict[str, Any] = {}

    class FakeSession:
        def close(self) -> None:
            calls["closed"] = True

    fake_db = FakeSession()
    monkeypatch.setattr(notification_service, "SessionLocal", lambda: fake_db)

    def fake_notify(db, event_type, title, body, details):
        calls["arguments"] = (db, event_type, title, body, details)
        return True

    monkeypatch.setattr(notification_service, "notify_event", fake_notify)
    assert notification_service.notify_event_background(
        "login", "用户登录", "user@example.com", {"ip": "127.0.0.1"}
    ) is True
    assert calls["arguments"] == (
        fake_db,
        "login",
        "用户登录",
        "user@example.com",
        {"ip": "127.0.0.1"},
    )
    assert calls["closed"] is True
