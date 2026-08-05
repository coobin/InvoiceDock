from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AppSetting, Invoice, JobLog, VerificationCache
from app.services import verifier


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _fake_settings(tmp_path):
    return SimpleNamespace(upload_dir=tmp_path, tz="Asia/Shanghai")


def test_verification_cache_roundtrip(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, "get_settings", lambda: _fake_settings(tmp_path))
    invoice = Invoice(
        original_name="a.pdf",
        stored_name="a.pdf",
        mime_type="application/pdf",
        file_size=1,
        sha256="a" * 64,
        invoice_number="24500000123456789012",
        total_amount=99.8,
        seller_name="示例销售方",
        kingdee_data={"raw": True},
    )
    with session_factory() as db:
        verifier._save_verification_cache(db, invoice, "piaozone")
        verifier._save_verification_cache(db, invoice, "piaozone")
        db.commit()
        rows = db.scalars(select(VerificationCache)).all()
        assert len(rows) == 1
        assert rows[0].verify_date == verifier._today_str()
        assert rows[0].method == "piaozone"
        assert rows[0].fields["total_amount"] == 99.8
        assert rows[0].kingdee_data == {"raw": True}
        cached = verifier._find_verification_cache(db, "24500000123456789012")
        assert cached is not None
        assert cached.invoice_number == "24500000123456789012"


def test_verification_cache_ignores_other_days(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, "get_settings", lambda: _fake_settings(tmp_path))
    with session_factory() as db:
        db.add(VerificationCache(invoice_number="11", verify_date="2000-01-01", method="kingdee"))
        db.commit()
        assert verifier._find_verification_cache(db, "11") is None


def test_process_invoice_reuses_today_cache_and_skips_provider(session_factory, tmp_path, monkeypatch):
    (tmp_path / "double.pdf").write_bytes(b"%PDF-1.4\n")
    provider_calls = []

    def fail_provider(*_args, **_kwargs):
        provider_calls.append("called")
        raise AssertionError("发票云不应被调用")

    with session_factory() as db:
        db.add(AppSetting(key="piaozone_enabled", value="true"))
        db.add(AppSetting(key="piaozone_base_url", value="https://example.com"))
        db.add(AppSetting(key="piaozone_client_id", value="cid"))
        db.add(AppSetting(key="piaozone_client_secret", value="secret"))
        invoice = Invoice(
            original_name="double.pdf",
            stored_name="double.pdf",
            mime_type="application/pdf",
            file_size=8,
            sha256="b" * 64,
            status="pending",
        )
        db.add(invoice)
        db.add(
            VerificationCache(
                invoice_number="24500000123456789012",
                verify_date=verifier._today_str(),
                method="piaozone",
                fields={
                    "invoice_type": "电子发票",
                    "invoice_number": "24500000123456789012",
                    "seller_name": "示例销售方",
                    "buyer_name": "示例购买方",
                    "total_amount": 99.8,
                    "category": "办公用品",
                },
                kingdee_data={"cached": True},
            )
        )
        db.commit()
        invoice_id = invoice.id

    monkeypatch.setattr(verifier, "SessionLocal", session_factory)
    monkeypatch.setattr(verifier, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(
        verifier,
        "extract_document",
        lambda path, mime: ("发票号码：24500000123456789012", {}, None),
    )
    monkeypatch.setattr(
        verifier,
        "parse_invoice_fields",
        lambda text, structured: {"invoice_number": "24500000123456789012"},
    )
    monkeypatch.setattr(verifier, "verify_with_piaozone", fail_provider)
    monkeypatch.setattr(verifier, "verify_with_kingdee", fail_provider)

    verifier.process_invoice(invoice_id)

    with session_factory() as db:
        invoice = db.get(Invoice, invoice_id)
        assert provider_calls == []
        assert invoice.status == "verified"
        assert invoice.verification_method == "piaozone"
        assert invoice.total_amount == 99.8
        assert invoice.kingdee_data == {"cached": True}
        assert db.scalar(select(JobLog).where(JobLog.event == "verify.cache_hit")) is not None


def test_provider_verification_writes_today_cache(session_factory, tmp_path, monkeypatch):
    (tmp_path / "fresh.pdf").write_bytes(b"%PDF-1.4\n")

    with session_factory() as db:
        db.add(AppSetting(key="piaozone_enabled", value="true"))
        db.add(AppSetting(key="piaozone_base_url", value="https://example.com"))
        db.add(AppSetting(key="piaozone_client_id", value="cid"))
        db.add(AppSetting(key="piaozone_client_secret", value="secret"))
        invoice = Invoice(
            original_name="fresh.pdf",
            stored_name="fresh.pdf",
            mime_type="application/pdf",
            file_size=8,
            sha256="c" * 64,
            status="pending",
        )
        db.add(invoice)
        db.commit()
        invoice_id = invoice.id

    monkeypatch.setattr(verifier, "SessionLocal", session_factory)
    monkeypatch.setattr(verifier, "get_settings", lambda: _fake_settings(tmp_path))
    monkeypatch.setattr(
        verifier,
        "extract_document",
        lambda path, mime: ("发票号码：99990000111122223333", {}, None),
    )
    monkeypatch.setattr(
        verifier,
        "parse_invoice_fields",
        lambda text, structured: {"invoice_number": "99990000111122223333"},
    )
    monkeypatch.setattr(
        verifier,
        "verify_with_piaozone",
        lambda path, config: (
            {
                "invoice_type": "增值税电子普通发票",
                "invoice_number": "99990000111122223333",
                "seller_name": "示例销售方",
                "buyer_name": "示例购买方",
                "total_amount": 12.34,
                "category": "未分类",
            },
            {"raw": True},
        ),
    )

    verifier.process_invoice(invoice_id)

    with session_factory() as db:
        invoice = db.get(Invoice, invoice_id)
        assert invoice.status == "verified"
        assert invoice.verification_method == "piaozone"
        row = db.scalar(
            select(VerificationCache).where(
                VerificationCache.invoice_number == "99990000111122223333"
            )
        )
        assert row is not None
        assert row.verify_date == verifier._today_str()
        assert row.fields["total_amount"] == 12.34
        assert row.kingdee_data == {"raw": True}
