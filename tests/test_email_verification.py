from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import config
from app.db import Base
from app.main import app, register_submit, send_verification_code_api
from app.models import EmailVerification, User, utcnow
from app.services.email_verification_service import (
    generate_6digit_code,
    send_registration_code,
    verify_registration_code,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


def test_generate_6digit_code():
    for _ in range(10):
        code = generate_6digit_code()
        assert len(code) == 6
        assert code.isdigit()
        assert 100000 <= int(code) <= 999999


def test_send_registration_code_not_configured(db_session, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            smtp_configured=False,
            require_email_verification=False,
        ),
    )
    ok, msg = send_registration_code(db_session, "test@example.com")
    assert ok is False
    assert "尚未配置" in msg


def test_send_and_verify_registration_code(db_session, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            smtp_configured=True,
            require_email_verification=True,
            app_name="InvoiceDock",
        ),
    )

    with patch("app.services.email_verification_service.send_smtp_email") as mock_send:
        # 1. First send -> succeeds
        ok, msg = send_registration_code(db_session, "alice@example.com", ip="127.0.0.1")
        assert ok is True
        assert "已发送" in msg
        assert mock_send.call_count == 1

        rec = db_session.scalar(select(EmailVerification).where(EmailVerification.email == "alice@example.com"))
        assert rec is not None
        assert rec.used is False
        code = rec.code

        # 2. Immediate second send within 60s -> rejected by rate limit
        ok2, msg2 = send_registration_code(db_session, "alice@example.com", ip="127.0.0.1")
        assert ok2 is False
        assert "60 秒" in msg2

        # 3. Verify with wrong code -> rejected
        ok_v1, err_v1 = verify_registration_code(db_session, "alice@example.com", "999999")
        assert ok_v1 is False
        assert "验证码错误" in err_v1

        # 4. Verify with correct code -> succeeds
        ok_v2, err_v2 = verify_registration_code(db_session, "alice@example.com", code)
        assert ok_v2 is True
        assert err_v2 == "ok"
        db_session.refresh(rec)
        assert rec.used is True

        # 5. Reuse already used code -> rejected
        ok_v3, err_v3 = verify_registration_code(db_session, "alice@example.com", code)
        assert ok_v3 is False


def test_expired_code_rejected(db_session, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            smtp_configured=True,
            require_email_verification=True,
        ),
    )
    rec = EmailVerification(
        email="expired@example.com",
        code="123456",
        expires_at=utcnow() - timedelta(minutes=1),
    )
    db_session.add(rec)
    db_session.commit()

    ok, err = verify_registration_code(db_session, "expired@example.com", "123456")
    assert ok is False
    assert "验证码错误或已过期" in err


@pytest.mark.asyncio
async def test_send_verification_code_api_and_registration(db_session, monkeypatch):
    test_settings = SimpleNamespace(
        registration_enabled=True,
        smtp_configured=True,
        require_email_verification=True,
        invite_required=False,
        app_name="InvoiceDock",
        registration_min_password_length=12,
        password_max_length=256,
        tz="Asia/Shanghai",
    )
    monkeypatch.setattr(config, "get_settings", lambda: test_settings)
    from app import main as app_main
    monkeypatch.setattr(app_main, "settings", test_settings)

    csrf = "email-csrf-token"
    session = {"csrf_token": csrf}

    with patch("app.services.email_verification_service.send_smtp_email"):
        # 1. Call API to send verification code
        req = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/auth/send-verification-code",
                "headers": [(b"host", b"testserver")],
                "session": session,
                "app": app,
                "router": app.router,
                "client": ("127.0.0.1", 12345),
            }
        )
        async def mock_form():
            return {"csrf_token": csrf, "email": "bob@example.com"}
        req.form = mock_form  # type: ignore

        resp = await send_verification_code_api(req, db_session)
        assert resp.status_code == 200
        import json
        data = json.loads(resp.body.decode())
        assert data["ok"] is True

        rec = db_session.scalar(select(EmailVerification).where(EmailVerification.email == "bob@example.com"))
        assert rec is not None
        code = rec.code

        # 2. Register without code -> fails
        bg = BackgroundTasks()
        reg_req = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/register",
                "headers": [(b"host", b"testserver")],
                "session": session,
                "app": app,
                "router": app.router,
                "client": ("127.0.0.1", 12345),
            }
        )
        async def mock_reg_form_no_code():
            return {
                "csrf_token": csrf,
                "email": "bob@example.com",
                "verification_code": "",
                "password": "Password123!456",
                "password_confirm": "Password123!456",
            }
        reg_req.form = mock_reg_form_no_code  # type: ignore
        resp_no_code = await register_submit(reg_req, bg, db_session)
        assert resp_no_code.status_code == 303
        assert resp_no_code.headers["location"] == "/register"
        assert db_session.scalar(select(User).where(User.email == "bob@example.com")) is None

        # 3. Register with correct code -> succeeds
        async def mock_reg_form_valid():
            return {
                "csrf_token": csrf,
                "email": "bob@example.com",
                "verification_code": code,
                "password": "Password123!456",
                "password_confirm": "Password123!456",
            }
        reg_req.form = mock_reg_form_valid  # type: ignore
        resp_valid = await register_submit(reg_req, bg, db_session)
        assert resp_valid.status_code == 303
        assert resp_valid.headers["location"] == "/admin"

        user = db_session.scalar(select(User).where(User.email == "bob@example.com"))
        assert user is not None
        assert user.email == "bob@example.com"
