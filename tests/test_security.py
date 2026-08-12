from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import security
from app.db import Base
from app.models import User
from app.security import (
    client_ip,
    current_user,
    is_reserved_username,
    rotate_user_sessions,
    session_auth_marker,
    throttle_limit,
    throttle_reset,
)


def test_throttle_allows_up_to_limit_then_blocks():
    key = "test:login-ip"
    throttle_reset(key)
    for _ in range(3):
        assert throttle_limit(key, 3, 60) is False
    assert throttle_limit(key, 3, 60) is True


def test_throttle_reset_clears_bucket():
    key = "test:register-ip"
    throttle_reset(key)
    for _ in range(3):
        throttle_limit(key, 3, 60)
    assert throttle_limit(key, 3, 60) is True
    throttle_reset(key)
    assert throttle_limit(key, 3, 60) is False


def test_throttle_keys_are_independent():
    throttle_reset("test:a")
    throttle_reset("test:b")
    throttle_limit("test:a", 1, 60)
    assert throttle_limit("test:a", 1, 60) is True
    assert throttle_limit("test:b", 1, 60) is False


def test_reserved_username_blocks_email_local_part_and_aliases(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(admin_username="kay", additional_reserved_usernames=set()),
    )
    assert is_reserved_username("admin@example.com") is True
    assert is_reserved_username("Admin+test@example.com") is True
    assert is_reserved_username("a.d_m-i-n@example.com") is True
    assert is_reserved_username("kay@example.com") is True
    assert is_reserved_username("normal-user@example.com") is False


def test_reserved_username_blocks_sensitive_display_names(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(admin_username="kay", additional_reserved_usernames=set()),
    )
    assert is_reserved_username("系统 管理员") is True
    assert is_reserved_username("官方") is True
    assert is_reserved_username("普通用户") is False


def test_environment_can_add_reserved_usernames(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(
            admin_username="kay",
            additional_reserved_usernames={"finance", "财务"},
        ),
    )
    assert is_reserved_username("finance@example.com") is True
    assert is_reserved_username("财务") is True


def _request(peer: str, forwarded: str = "") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 1234),
            "session": {},
        }
    )


def test_client_ip_ignores_forwarded_header_from_untrusted_peer(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(trusted_proxy_networks=()),
    )
    assert client_ip(_request("203.0.113.8", "1.2.3.4")) == "203.0.113.8"


def test_client_ip_walks_only_trusted_proxy_chain(monkeypatch):
    from ipaddress import ip_network

    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(
            trusted_proxy_networks=(ip_network("10.0.0.0/8"),),
        ),
    )
    assert client_ip(_request("10.0.0.2", "198.51.100.9, 10.0.0.3")) == "198.51.100.9"


def test_rotating_session_version_invalidates_existing_session(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(app_secret="test-secret"),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="session-user", email="session@example.com")
        db.add(user)
        db.commit()
        request = _request("127.0.0.1")
        request.scope["session"] = {
            "user_id": user.id,
            "auth_marker": session_auth_marker(user),
        }
        assert current_user(request, db) is user
        rotate_user_sessions(user)
        db.commit()
        assert current_user(request, db) is None
        assert request.session == {}
