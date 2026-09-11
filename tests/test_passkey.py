import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import ResidentKeyRequirement

from app.db import Base
from app.main import (
    app,
    passkey_delete,
    passkey_login_options,
    passkey_login_verify,
    passkey_register_options,
    passkey_register_verify,
)
from app.models import User, UserPasskey
from app.security import session_auth_marker
from app.services.passkey_service import (
    generate_auth_options,
    generate_reg_options,
    get_origins,
    get_rp_id,
)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'passkey_test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _request(
    user: User | None = None,
    path: str = "/auth/passkey/login/options",
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
    session: dict | None = None,
    json_body: dict | None = None,
    form_data: dict | None = None,
) -> Request:
    sess = dict(session or {})
    if user:
        sess["user_id"] = user.id
        sess["auth_marker"] = session_auth_marker(user)

    hdrs = list(headers or [])
    if json_body is not None:
        body_bytes = json.dumps(json_body).encode("utf-8")
        hdrs.append((b"content-type", b"application/json"))
    else:
        body_bytes = b""

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"localhost:8000"), *hdrs],
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 8000),
        "session": sess,
        "app": app,
        "router": app.router,
    }
    req = Request(scope)

    if json_body is not None:
        async def _receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        req._receive = _receive

    if form_data is not None:
        async def _form():
            return form_data
        req.form = _form

    return req


def test_generate_reg_options_requires_resident_key():
    user = User(username="test_user", display_name="测试用户")
    opts = generate_reg_options(user, "localhost", [])
    # 必须启用 REQUIRED resident_key 以便支持免用户名 Discoverable Credential
    assert opts.authenticator_selection.resident_key == ResidentKeyRequirement.REQUIRED
    assert opts.rp.id == "localhost"
    assert opts.user.name == "test_user"
    assert opts.exclude_credentials == []


def test_generate_auth_options_discoverable():
    opts = generate_auth_options("localhost")
    assert opts.rp_id == "localhost"
    # 免用户名 Discoverable Credentials 不限制 allow_credentials
    assert opts.allow_credentials is None or len(opts.allow_credentials) == 0


def test_get_rp_id_and_origins():
    req = _request(headers=[(b"x-forwarded-host", b"dock.company.com:443")])
    assert get_rp_id(req) == "dock.company.com"

    origins = get_origins(req)
    assert any("dock.company.com" in o for o in origins)


def test_passkey_register_options_unauthenticated(tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        req = _request(user=None, path="/auth/passkey/register/options")
        with pytest.raises(HTTPException) as exc_info:
            import asyncio
            asyncio.run(passkey_register_options(req, db))
        assert exc_info.value.status_code in (303, 401, 403)


def test_passkey_register_flow(tmp_path):
    factory = _factory(tmp_path)
    import asyncio

    with factory() as db:
        user = User(username="alice", email="alice@example.com")
        db.add(user)
        db.commit()

        # 1. 获取 options
        req1 = _request(user=user, path="/auth/passkey/register/options")
        resp1 = asyncio.run(passkey_register_options(req1, db))
        assert resp1.status_code == 200
        options_data = json.loads(resp1.body)
        assert "challenge" in options_data
        challenge = req1.session["passkey_reg_challenge"]
        assert challenge

        # 2. 验证凭据并保存
        mock_verified = MagicMock()
        mock_verified.credential_id = b"cred_id_123456"
        mock_verified.credential_public_key = b"pub_key_123456"
        mock_verified.sign_count = 0
        mock_verified.aaguid = "00000000-0000-0000-0000-000000000000"

        req2 = _request(
            user=user,
            path="/auth/passkey/register/verify",
            session={"passkey_reg_challenge": challenge},
            json_body={"name": "我的 MacBook", "response": {"transports": ["internal"]}},
        )

        with patch("app.main.verify_reg_response", return_value=mock_verified):
            resp2 = asyncio.run(passkey_register_verify(req2, db))
            assert resp2["ok"] is True

        # 检查数据库中已创建 UserPasskey
        passkey = db.query(UserPasskey).filter_by(user_id=user.id).first()
        assert passkey is not None
        assert passkey.name == "我的 MacBook"
        assert passkey.credential_id == bytes_to_base64url(b"cred_id_123456")
        assert passkey.sign_count == 0


def test_passkey_login_usernameless_flow(tmp_path):
    factory = _factory(tmp_path)
    import asyncio

    with factory() as db:
        user = User(username="bob", email="bob@example.com")
        db.add(user)
        db.commit()

        raw_cred_id = b"cred_device_bob"
        cred_b64 = bytes_to_base64url(raw_cred_id)
        pk = UserPasskey(
            user_id=user.id,
            name="Bob 的指纹",
            credential_id=cred_b64,
            public_key="mock_pubkey",
            sign_count=5,
        )
        db.add(pk)
        db.commit()

        # 1. 免用户名获取登录选项
        req1 = _request(path="/auth/passkey/login/options")
        resp1 = asyncio.run(passkey_login_options(req1))
        assert resp1.status_code == 200
        auth_challenge = req1.session["passkey_auth_challenge"]
        assert auth_challenge

        # 2. 设备识别并返回包含 credential_id 的凭据（不需要提供 username）
        mock_auth_verified = MagicMock()
        mock_auth_verified.new_sign_count = 6

        req2 = _request(
            path="/auth/passkey/login/verify",
            session={"passkey_auth_challenge": auth_challenge},
            json_body={"id": cred_b64, "next": "/projects"},
        )

        bg = BackgroundTasks()
        with patch("app.main.verify_auth_response", return_value=mock_auth_verified):
            resp2 = asyncio.run(passkey_login_verify(req2, bg, db))
            assert resp2["ok"] is True
            assert resp2["redirect"] == "/projects"

        # 验证会话已赋予对应用户
        assert req2.session.get("user_id") == user.id

        # 验证凭据计数与最后使用时间已更新
        db.refresh(pk)
        assert pk.sign_count == 6
        assert pk.last_used_at is not None


def test_passkey_login_fails_on_unknown_credential(tmp_path):
    factory = _factory(tmp_path)
    import asyncio

    with factory() as db:
        req = _request(
            path="/auth/passkey/login/verify",
            session={"passkey_auth_challenge": "some_challenge"},
            json_body={"id": "non_existent_cred_id"},
        )
        bg = BackgroundTasks()
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(passkey_login_verify(req, bg, db))
        assert exc_info.value.status_code == 400
        assert "未找到该设备对应的通行密钥" in exc_info.value.detail


def test_passkey_delete_permission_and_isolation(tmp_path):
    factory = _factory(tmp_path)
    import asyncio

    with factory() as db:
        alice = User(username="alice", email="alice@example.com")
        bob = User(username="bob", email="bob@example.com")
        admin = User(username="admin", email="admin@example.com", role="admin")
        db.add_all([alice, bob, admin])
        db.commit()

        pk_alice = UserPasskey(user_id=alice.id, name="Alice Key", credential_id="alice_cred", public_key="pk")
        pk_bob = UserPasskey(user_id=bob.id, name="Bob Key", credential_id="bob_cred", public_key="pk")
        db.add_all([pk_alice, pk_bob])
        db.commit()

        # Alice 尝试删除 Bob 的 passkey -> 失败 404
        req_alice_bad = _request(
            user=alice,
            path=f"/auth/passkey/{pk_bob.id}/delete",
            session={"csrf_token": "valid_token"},
            form_data={"csrf_token": "valid_token"},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(passkey_delete(pk_bob.id, req_alice_bad, db))
        assert exc.value.status_code == 404

        # Alice 成功删除自己的 passkey
        req_alice_ok = _request(
            user=alice,
            path=f"/auth/passkey/{pk_alice.id}/delete",
            session={"csrf_token": "valid_token"},
            form_data={"csrf_token": "valid_token"},
        )
        resp = asyncio.run(passkey_delete(pk_alice.id, req_alice_ok, db))
        assert resp.status_code == 303
        assert db.get(UserPasskey, pk_alice.id) is None
