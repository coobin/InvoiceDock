from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import config
from app.db import Base
from app.main import (
    OAuthLoginDenied,
    _oauth_user_for_provider,
    app,
)
from app.models import InviteCode, User, utcnow
from app.security import hash_password
from app.services.invite_service import (
    create_invite_code,
    disable_invite_code,
    generate_random_invite_code,
    validate_and_consume_invite_code,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


def test_generate_random_invite_code():
    code = generate_random_invite_code("TEST-", 6)
    assert code.startswith("TEST-")
    assert len(code) == 11


def test_invite_code_lifecycle(db_session, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            invite_required=True,
            registration_invite_codes="STATIC-VIP,TEST-CODE",
        ),
    )

    # 1. Environment variable preset code
    ok, reason = validate_and_consume_invite_code(db_session, "static-vip")
    assert ok is True
    assert reason == "env_preset"

    # 2. Empty code
    ok, reason = validate_and_consume_invite_code(db_session, "")
    assert ok is False
    assert "请输入邀请码" in reason

    # 3. Create database invite code with max_uses=2
    invite = create_invite_code(db_session, code="MY-CODE-2026", max_uses=2, note="For test")
    assert invite.code == "MY-CODE-2026"
    assert invite.status == "active"
    assert invite.used_count == 0

    # 4. Use it 1st time
    ok, reason = validate_and_consume_invite_code(db_session, "my-code-2026")
    assert ok is True
    assert reason == f"db:{invite.id}"
    db_session.refresh(invite)
    assert invite.used_count == 1
    assert invite.status == "active"

    # 5. Use it 2nd time (reaches limit)
    ok, reason = validate_and_consume_invite_code(db_session, "MY-CODE-2026")
    assert ok is True
    db_session.refresh(invite)
    assert invite.used_count == 2
    assert invite.status == "depleted"

    # 6. 3rd time should fail
    ok, reason = validate_and_consume_invite_code(db_session, "MY-CODE-2026")
    assert ok is False
    assert "已达最大使用次数" in reason


def test_invite_code_expiration(db_session, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            invite_required=True,
            registration_invite_codes="",
        ),
    )
    past = utcnow() - timedelta(days=1)
    invite = create_invite_code(db_session, code="EXPIRED-CODE", expires_at=past)

    ok, reason = validate_and_consume_invite_code(db_session, "EXPIRED-CODE")
    assert ok is False
    assert "已过期" in reason
    db_session.refresh(invite)
    assert invite.status == "expired"


def test_invite_code_disable(db_session, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            invite_required=True,
            registration_invite_codes="",
        ),
    )
    invite = create_invite_code(db_session, code="DISABLE-ME")
    assert disable_invite_code(db_session, invite.id) is True
    db_session.refresh(invite)
    assert invite.status == "disabled"

    ok, reason = validate_and_consume_invite_code(db_session, "DISABLE-ME")
    assert ok is False
    assert "已被停用" in reason


def test_oauth_user_for_provider_new_user(db_session):
    user, created = _oauth_user_for_provider(
        db=db_session,
        provider="github",
        subject_id="123456",
        email="octo@example.com",
        name="Octocat",
        preferred_username="octocat",
    )
    assert created is True
    assert user.oidc_subject == "github|123456"
    assert user.email == "octo@example.com"
    assert user.username == "octocat"
    assert user.display_name == "Octocat"
    assert user.role == "member"

    # Re-login with same GitHub user
    user2, created2 = _oauth_user_for_provider(
        db=db_session,
        provider="github",
        subject_id="123456",
        email="octo@example.com",
        name="Octocat Updated",
        preferred_username="octocat",
    )
    assert created2 is False
    assert user2.id == user.id


def test_oauth_user_binds_existing_local_user(db_session):
    local = User(
        username="localuser",
        email="alice@example.com",
        display_name="Alice L",
        password_hash=hash_password("Password123!456"),
        role="member",
    )
    db_session.add(local)
    db_session.commit()

    user, created = _oauth_user_for_provider(
        db=db_session,
        provider="google",
        subject_id="goog-789",
        email="alice@example.com",
        name="Alice Google",
    )
    assert created is False
    assert user.id == local.id
    assert user.oidc_subject == "google|goog-789"


def test_oauth_user_denies_conflict_and_inactive(db_session):
    # Inactive user
    inactive = User(
        username="inactive_user",
        email="bad@example.com",
        active=False,
    )
    db_session.add(inactive)
    db_session.commit()

    with pytest.raises(OAuthLoginDenied) as exc:
        _oauth_user_for_provider(
            db=db_session,
            provider="github",
            subject_id="gh-bad",
            email="bad@example.com",
            name="Bad",
        )
    assert exc.value.reason == "inactive"

    # Conflicting provider user
    google_user = User(
        username="google_user",
        email="multi@example.com",
        oidc_subject="google|111222",
    )
    db_session.add(google_user)
    db_session.commit()

    with pytest.raises(OAuthLoginDenied) as exc2:
        _oauth_user_for_provider(
            db=db_session,
            provider="github",
            subject_id="gh-333",
            email="multi@example.com",
            name="Multi",
        )
    assert exc2.value.reason == "email_conflict"


@pytest.mark.asyncio
async def test_oauth_endpoints_raise_404_when_disabled():
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.main import github_login, google_login

    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/google/login",
            "headers": [(b"host", b"testserver")],
            "query_string": b"",
        }
    )
    with pytest.raises(HTTPException) as exc1:
        await google_login(req)
    assert exc1.value.status_code == 404

    with pytest.raises(HTTPException) as exc2:
        await github_login(req)
    assert exc2.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_invites_views_and_actions(db_session):
    from starlette.requests import Request

    from app.main import admin_create_invite, admin_disable_invite, admin_invites_page
    from app.security import session_auth_marker

    admin = User(username="admin", role="admin", email="admin@example.com")
    db_session.add(admin)
    db_session.commit()

    csrf = "test-csrf-token"
    session = {"user_id": admin.id, "auth_marker": session_auth_marker(admin), "csrf_token": csrf}

    # 1. GET /admin/invites
    get_scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/invites",
        "raw_path": b"/admin/invites",
        "headers": [(b"host", b"testserver")],
        "query_string": b"",
        "session": session,
        "app": app,
        "router": app.router,
        "client": ("127.0.0.1", 1234),
    }
    get_req = Request(get_scope)
    resp = admin_invites_page(get_req, db_session)
    assert resp.status_code == 200

    # 2. POST /admin/invites (Create)
    post_scope = {
        "type": "http",
        "method": "POST",
        "path": "/admin/invites",
        "raw_path": b"/admin/invites",
        "headers": [(b"host", b"testserver")],
        "query_string": b"",
        "session": session,
        "app": app,
        "router": app.router,
        "client": ("127.0.0.1", 1234),
    }
    post_req = Request(post_scope)
    async def mock_form():
        return {"csrf_token": csrf, "code": "VIP-TEST-99", "max_uses": "5", "note": "Integration test"}
    post_req.form = mock_form  # type: ignore

    create_resp = await admin_create_invite(post_req, db_session)
    assert create_resp.status_code == 303
    assert create_resp.headers["location"] == "/admin/invites"

    created = db_session.scalar(select(InviteCode).where(InviteCode.code == "VIP-TEST-99"))
    assert created is not None
    assert created.max_uses == 5
    assert created.status == "active"

    # 3. POST /admin/invites/{id}/disable
    disable_scope = {
        "type": "http",
        "method": "POST",
        "path": f"/admin/invites/{created.id}/disable",
        "raw_path": f"/admin/invites/{created.id}/disable".encode(),
        "headers": [(b"host", b"testserver")],
        "query_string": b"",
        "session": session,
        "app": app,
        "router": app.router,
        "client": ("127.0.0.1", 1234),
    }
    disable_req = Request(disable_scope)
    async def mock_disable_form():
        return {"csrf_token": csrf}
    disable_req.form = mock_disable_form  # type: ignore

    dis_resp = await admin_disable_invite(created.id, disable_req, db_session)
    assert dis_resp.status_code == 303
    db_session.refresh(created)
    assert created.status == "disabled"


@pytest.mark.asyncio
async def test_registration_with_invite_code(db_session, monkeypatch):
    from fastapi import BackgroundTasks

    from app.main import register_submit
    from app.services.invite_service import create_invite_code

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            registration_enabled=True,
            invite_required=True,
            registration_invite_codes="",
            registration_min_password_length=12,
            password_max_length=256,
        ),
    )

    invite = create_invite_code(db_session, code="REG-TEST-VIP", max_uses=1)
    csrf = "reg-csrf"
    bg = BackgroundTasks()

    # 1. Missing invite code
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/register",
        "raw_path": b"/register",
        "headers": [(b"host", b"testserver")],
        "query_string": b"",
        "session": {"csrf_token": csrf},
        "app": app,
        "router": app.router,
        "client": ("127.0.0.1", 1234),
    }
    req = Request(scope)
    async def mock_form_no_invite():
        return {
            "csrf_token": csrf,
            "invite_code": "",
            "email": "user1@example.com",
            "password": "Password123!456",
            "password_confirm": "Password123!456",
        }
    req.form = mock_form_no_invite  # type: ignore

    resp1 = await register_submit(req, bg, db_session)
    assert resp1.status_code == 303
    assert resp1.headers["location"] == "/register"
    assert db_session.scalar(select(User).where(User.email == "user1@example.com")) is None

    # 2. Valid invite code
    async def mock_form_with_invite():
        return {
            "csrf_token": csrf,
            "invite_code": "reg-test-vip",
            "email": "user1@example.com",
            "password": "Password123!456",
            "password_confirm": "Password123!456",
        }
    req.form = mock_form_with_invite  # type: ignore

    resp2 = await register_submit(req, bg, db_session)
    assert resp2.status_code == 303
    assert resp2.headers["location"] == "/admin"

    user = db_session.scalar(select(User).where(User.email == "user1@example.com"))
    assert user is not None
    assert user.role == "member"
    db_session.refresh(invite)
    assert invite.used_count == 1
    assert invite.status == "depleted"


