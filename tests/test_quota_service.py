from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import User
from app.services import quota_service


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    monkeypatch.setattr(
        quota_service,
        "get_settings",
        lambda: SimpleNamespace(tz="Asia/Shanghai"),
    )
    with factory() as db:
        yield db


def _user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com")
    db.add(user)
    db.commit()
    return user


def test_daily_limit_defaults_to_fifty_and_can_be_changed(db_session):
    assert quota_service.get_tax_verify_daily_limit(db_session) == 50
    assert quota_service.set_tax_verify_daily_limit(db_session, "75") == 75
    assert quota_service.get_tax_verify_daily_limit(db_session) == 75


@pytest.mark.parametrize("value", ["", "x", "0", "10001"])
def test_daily_limit_rejects_invalid_values(db_session, value):
    with pytest.raises(ValueError):
        quota_service.set_tax_verify_daily_limit(db_session, value)


def test_reservation_is_per_user_and_per_day(db_session):
    first = _user(db_session, "first")
    second = _user(db_session, "second")
    quota_service.set_tax_verify_daily_limit(db_session, 2)

    assert quota_service.reserve_tax_verification(db_session, first.id, "2026-08-12") == (
        True,
        1,
        2,
    )
    assert quota_service.reserve_tax_verification(db_session, first.id, "2026-08-12") == (
        True,
        2,
        2,
    )
    assert quota_service.reserve_tax_verification(db_session, first.id, "2026-08-12") == (
        False,
        2,
        2,
    )
    assert quota_service.reserve_tax_verification(db_session, second.id, "2026-08-12") == (
        True,
        1,
        2,
    )
    assert quota_service.reserve_tax_verification(db_session, first.id, "2026-08-13") == (
        True,
        1,
        2,
    )


def test_ownerless_legacy_invoice_does_not_consume_user_quota(db_session):
    quota_service.set_tax_verify_daily_limit(db_session, 1)
    assert quota_service.reserve_tax_verification(db_session, None) == (True, 0, 1)
