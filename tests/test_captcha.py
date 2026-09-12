from __future__ import annotations

import asyncio

from fastapi import BackgroundTasks, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SqlSession
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import app, login_submit
from app.models import User
from app.security import (
    CAPTCHA_FAILURE_THRESHOLD,
    get_login_failures,
    hash_password,
    is_captcha_required,
    record_login_failure,
    reset_login_failures,
)
from app.services.captcha_service import (
    generate_captcha_svg,
    generate_captcha_text,
    store_captcha,
    verify_captcha,
)


def _mock_request(session: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/auth/captcha",
        "raw_path": b"/auth/captcha",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "session": session if session is not None else {},
    }
    return Request(scope)


def test_generate_captcha_text():
    text1 = generate_captcha_text(4)
    text2 = generate_captcha_text(4)
    assert len(text1) == 4
    assert len(text2) == 4
    for ch in text1:
        assert ch not in "0O1Il"


def test_generate_captcha_svg():
    text = "K9XZ"
    svg = generate_captcha_svg(text)
    assert "<svg" in svg
    assert "</svg>" in svg
    assert "K" in svg
    assert "9" in svg
    assert "X" in svg
    assert "Z" in svg


def test_captcha_session_lifecycle():
    req = _mock_request()
    store_captcha(req, "ABCD")
    assert req.session.get("captcha_code") == "ABCD"
    assert "captcha_time" in req.session

    assert verify_captcha(req, "WRONG") is False
    assert "captcha_code" not in req.session

    store_captcha(req, "Ef23")
    assert verify_captcha(req, "ef23") is True
    assert "captcha_code" not in req.session


def test_login_failure_count_and_captcha_threshold():
    ip = "192.168.10.99"
    user = "alice_test"
    reset_login_failures(ip, user)

    req = _mock_request()
    assert is_captcha_required(req, ip, user) is False

    cnt1 = record_login_failure(ip, user)
    assert cnt1 == 1
    assert get_login_failures(ip, user) == 1
    assert is_captcha_required(req, ip, user) is False

    cnt2 = record_login_failure(ip, user)
    assert cnt2 >= CAPTCHA_FAILURE_THRESHOLD
    assert is_captcha_required(req, ip, user) is True

    reset_login_failures(ip, user)
    assert get_login_failures(ip, user) == 0
    assert is_captcha_required(req, ip, user) is False


def _db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'captcha_test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=SqlSession, expire_on_commit=False)


def _submit_request(path: str = "/admin", session: dict | None = None, form_data: dict | None = None) -> Request:
    sess = dict(session or {})
    sess["csrf_token"] = "mock_csrf"
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("10.10.10.20", 54321),
        "server": ("testserver", 80),
        "session": sess,
        "app": app,
        "router": app.router,
    }
    req = Request(scope)
    async def _form():
        return form_data or {}
    req.form = _form
    return req


def test_login_flow_with_smart_captcha(tmp_path):
    factory = _db_factory(tmp_path)
    with factory() as db:
        user = User(
            username="bob_sec",
            email="bob@example.com",
            password_hash=hash_password("SuperSecret1234"),
            active=True,
        )
        db.add(user)
        db.commit()

        reset_login_failures("10.10.10.20", "bob_sec")
        bg = BackgroundTasks()

        # 1. 第一次输错密码 -> 失败，但不需要验证码
        req1 = _submit_request(form_data={"csrf_token": "mock_csrf", "username": "bob_sec", "password": "wrong"})
        resp1 = asyncio.run(login_submit(req1, bg, db))
        assert resp1.status_code == 303
        assert get_login_failures("10.10.10.20", "bob_sec") == 1

        # 2. 第二次输错密码 -> 失败，达到阈值，触发验证码标记
        req2 = _submit_request(session=req1.session, form_data={"csrf_token": "mock_csrf", "username": "bob_sec", "password": "wrong"})
        resp2 = asyncio.run(login_submit(req2, bg, db))
        assert resp2.status_code == 303
        assert req2.session.get("require_captcha") is True

        # 3. 此时不传验证码提交正确密码 -> 被验证码拦截
        req3 = _submit_request(session=req2.session, form_data={"csrf_token": "mock_csrf", "username": "bob_sec", "password": "SuperSecret1234"})
        resp3 = asyncio.run(login_submit(req3, bg, db))
        assert resp3.status_code == 303
        assert "user_id" not in req3.session

        # 4. 输入正确验证码与正确密码 -> 登录成功
        store_captcha(req3, "K89A")
        req4 = _submit_request(
            session=req3.session,
            form_data={
                "csrf_token": "mock_csrf",
                "username": "bob_sec",
                "password": "SuperSecret1234",
                "captcha": "k89a",
            },
        )
        resp4 = asyncio.run(login_submit(req4, bg, db))
        assert resp4.status_code == 303
        assert req4.session.get("user_id") == user.id
        assert get_login_failures("10.10.10.20", "bob_sec") == 0
        assert req4.session.get("require_captcha") is None
