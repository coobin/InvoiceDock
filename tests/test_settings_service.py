from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.services import settings_service


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as db:
        yield db


def _fake_settings(**overrides):
    values = {
        "oidc_enabled": True,
        "oidc_issuer": "https://auth.example.com",
        "oidc_client_id": "client-id",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_oidc_enabled_defaults_to_env_configuration(monkeypatch, db_session):
    monkeypatch.setattr(settings_service, "get_settings", lambda: _fake_settings())
    assert settings_service.oidc_enabled(db_session) is True


def test_oidc_enabled_respects_admin_toggle(monkeypatch, db_session):
    monkeypatch.setattr(settings_service, "get_settings", lambda: _fake_settings())
    settings_service.set_value(db_session, settings_service.OIDC_TOGGLE_KEY, "false")
    db_session.commit()
    assert settings_service.oidc_enabled(db_session) is False
    settings_service.set_value(db_session, settings_service.OIDC_TOGGLE_KEY, "true")
    db_session.commit()
    assert settings_service.oidc_enabled(db_session) is True


def test_oidc_enabled_requires_env_oidc_config(monkeypatch, db_session):
    monkeypatch.setattr(
        settings_service,
        "get_settings",
        lambda: _fake_settings(oidc_enabled=False),
    )
    assert settings_service.oidc_enabled(db_session) is False
