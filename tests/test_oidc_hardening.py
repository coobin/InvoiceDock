from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import main
from app.db import Base
from app.main import OIDCLoginDenied, _oidc_user_for_claims
from app.models import User


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


@pytest.fixture(autouse=True)
def oidc_settings(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            oidc_issuer="https://id.example.com",
            oidc_domains={"example.com"},
            oidc_group_claim="groups",
            oidc_admin_group="invoice-admins",
        ),
    )


def test_oidc_never_silently_binds_existing_local_email(db_session):
    local = User(
        username="person@example.com",
        email="person@example.com",
        password_hash="local-password-hash",
    )
    db_session.add(local)
    db_session.commit()

    with pytest.raises(OIDCLoginDenied) as exc:
        _oidc_user_for_claims(
            db_session,
            {
                "sub": "remote-subject",
                "email": "person@example.com",
                "email_verified": True,
            },
        )
    assert exc.value.reason == "local_email_conflict"
    db_session.refresh(local)
    assert local.oidc_subject is None
    assert local.password_hash == "local-password-hash"


def test_oidc_requires_verified_email_for_allowed_domain(db_session):
    with pytest.raises(OIDCLoginDenied) as exc:
        _oidc_user_for_claims(
            db_session,
            {
                "sub": "unverified-subject",
                "email": "person@example.com",
                "email_verified": False,
            },
        )
    assert exc.value.reason == "email_unverified"


def test_oidc_role_is_derived_from_configured_group(db_session):
    user, created = _oidc_user_for_claims(
        db_session,
        {
            "sub": "admin-subject",
            "email": "oidc-admin@example.com",
            "email_verified": True,
            "groups": ["invoice-admins"],
        },
    )
    assert created is True
    assert user.oidc_subject == "https://id.example.com|admin-subject"
    assert user.role == "admin"
    assert user.password_hash is None
