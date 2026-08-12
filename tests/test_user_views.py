from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.db import Base
from app.main import _dashboard_job_logs, app, integrations_page
from app.models import JobLog, User


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'views.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _request(user: User, path: str = "/integrations") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 443),
            "session": {"user_id": user.id},
            "app": app,
            "router": app.router,
        }
    )


def test_member_dashboard_only_shows_own_job_logs(tmp_path):
    factory = _factory(tmp_path)
    now = datetime(2026, 8, 12, 10, 0, 0)
    with factory() as db:
        first = User(username="first", email="first@example.com")
        second = User(username="second", email="second@example.com")
        admin = User(username="kay", email="admin@example.com", role="admin")
        db.add_all([first, second, admin])
        db.flush()
        db.add_all(
            [
                JobLog(user_id=first.id, event="first", message="first", created_at=now),
                JobLog(user_id=second.id, event="second", message="second", created_at=now + timedelta(minutes=1)),
                JobLog(event="system", message="system", created_at=now + timedelta(minutes=2)),
            ]
        )
        db.commit()

        assert [row.event for row in _dashboard_job_logs(db, first)] == ["first"]
        assert [row.event for row in _dashboard_job_logs(db, second)] == ["second"]
        assert [row.event for row in _dashboard_job_logs(db, admin)] == [
            "system",
            "second",
            "first",
        ]


def test_member_integrations_only_show_tax_switch_and_llm(tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        member = User(username="member", email="member@example.com")
        admin = User(username="kay", email="admin@example.com", role="admin")
        db.add_all([member, admin])
        db.commit()

        member_html = integrations_page(_request(member), db).body.decode()
        assert "税务发票云验票" in member_html
        assert 'name="tax_verify_enabled"' in member_html
        assert "OCR + LLM 双源一致性" in member_html
        assert "税务发票云 · 识别查验" not in member_html
        assert "税务发票云 · 标准版" not in member_html
        assert "/integrations/test/kingdee" not in member_html
        assert "/integrations/test/piaozone" not in member_html
        assert "/integrations/test/llm" in member_html

        admin_html = integrations_page(_request(admin), db).body.decode()
        assert "税务发票云 · 识别查验" in admin_html
        assert "税务发票云 · 标准版" in admin_html
