from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AppSetting, UserIntegration
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


def test_user_tax_verification_switch_defaults_on_and_can_be_disabled(db_session):
    user_id = "user-tax-switch"
    assert settings_service.get_user_tax_verify_enabled(db_session, user_id) is True
    settings_service.set_user_tax_verify_enabled(db_session, user_id, False)
    assert settings_service.get_user_tax_verify_enabled(db_session, user_id) is False
    assert settings_service.get_integrations(db_session, user_id=user_id)["verify_provider"] == "false"


def test_global_tax_verification_off_cannot_be_overridden_by_user(db_session):
    user_id = "user-tax-global-off"
    db_session.add(AppSetting(key="verify_provider", value="false"))
    db_session.commit()
    settings_service.set_user_tax_verify_enabled(db_session, user_id, True)
    assert settings_service.get_integrations(db_session, user_id=user_id)["verify_provider"] == "false"


def test_only_llm_credentials_are_user_overridable(db_session):
    user_id = "user-custom-integrations"
    db_session.add_all(
        [
            UserIntegration(user_id=user_id, key="kingdee_enabled", value="true"),
            UserIntegration(user_id=user_id, key="kingdee_base_url", value="https://user-tax.example.com"),
            UserIntegration(user_id=user_id, key="llm_enabled", value="true"),
            UserIntegration(user_id=user_id, key="llm_base_url", value="https://user-llm.example.com"),
        ]
    )
    db_session.commit()

    values = settings_service.get_integrations(db_session, user_id=user_id)
    assert values["kingdee_base_url"] != "https://user-tax.example.com"
    assert values["llm_base_url"] == "https://user-llm.example.com"
    assert settings_service.user_custom_integrations(db_session, user_id) == {"llm"}


def test_database_value_is_not_shadowed_by_a_model_default(monkeypatch, db_session):
    configured = SimpleNamespace(
        model_fields_set=set(),
        **settings_service.INTEGRATION_DEFAULTS,
    )
    monkeypatch.setattr(settings_service, "get_settings", lambda: configured)
    db_session.add(AppSetting(key="llm_model", value="database-model"))
    db_session.commit()

    assert settings_service.get_integrations(db_session)["llm_model"] == "database-model"


def test_explicit_environment_value_wins_over_database(monkeypatch, db_session):
    configured = SimpleNamespace(
        model_fields_set={"llm_model"},
        **{**settings_service.INTEGRATION_DEFAULTS, "llm_model": "environment-model"},
    )
    monkeypatch.setattr(settings_service, "get_settings", lambda: configured)
    db_session.add(AppSetting(key="llm_model", value="database-model"))
    db_session.commit()

    assert settings_service.get_integrations(db_session)["llm_model"] == "environment-model"
