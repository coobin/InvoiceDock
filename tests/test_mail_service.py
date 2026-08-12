from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Invoice, User
from app.services import mail_service


def _fake_db() -> SimpleNamespace:
    db = SimpleNamespace(deleted=[], added=[])
    db.add = db.added.append  # type: ignore[attr-defined]
    db.delete = db.deleted.append  # type: ignore[attr-defined]
    db.commit = lambda: None  # type: ignore[attr-defined]
    return db


def test_discard_non_invoice_removes_record_and_file(tmp_path, monkeypatch) -> None:
    uploads = tmp_path / "uploads"
    previews = tmp_path / "previews"
    uploads.mkdir()
    previews.mkdir()
    monkeypatch.setattr(
        mail_service, "get_settings", lambda: SimpleNamespace(upload_dir=uploads, preview_dir=previews)
    )
    db = _fake_db()
    invoice = SimpleNamespace(
        id="1", stored_name="x.pdf", original_name="通知.pdf",
        invoice_number="", total_amount=None, seller_name="", buyer_name="",
    )
    (uploads / "x.pdf").write_bytes(b"%PDF")
    assert mail_service._discard_non_invoice(db, invoice) is True
    assert db.deleted == [invoice]
    assert not (uploads / "x.pdf").exists()


def test_discard_keeps_documents_with_invoice_identity(tmp_path, monkeypatch) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(
        mail_service, "get_settings", lambda: SimpleNamespace(upload_dir=uploads, preview_dir=tmp_path / "previews")
    )
    db = _fake_db()
    invoice = SimpleNamespace(
        id="2", stored_name="y.pdf", original_name="发票.pdf",
        invoice_number="12345678", total_amount=None, seller_name="", buyer_name="",
    )
    (uploads / "y.pdf").write_bytes(b"%PDF")
    assert mail_service._discard_non_invoice(db, invoice) is False
    assert db.deleted == []
    assert (uploads / "y.pdf").exists()


def test_discard_not_cloud_verified_removes_record_and_files(tmp_path, monkeypatch) -> None:
    uploads = tmp_path / "uploads"
    previews = tmp_path / "previews"
    uploads.mkdir()
    previews.mkdir()
    monkeypatch.setattr(
        mail_service, "get_settings", lambda: SimpleNamespace(upload_dir=uploads, preview_dir=previews)
    )
    db = _fake_db()
    invoice = SimpleNamespace(id="3", stored_name="z.pdf", original_name="回单.pdf")
    (uploads / "z.pdf").write_bytes(b"%PDF")
    (previews / "3.jpg").write_bytes(b"image")
    mail_service._discard_not_cloud_verified(db, invoice)
    assert db.deleted == [invoice]
    assert not (uploads / "z.pdf").exists()
    assert not (previews / "3.jpg").exists()


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dedupe.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:  # type: ignore[no-untyped-def]
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def _setup_dirs(tmp_path):
    uploads = tmp_path / "uploads"
    previews = tmp_path / "previews"
    uploads.mkdir()
    previews.mkdir()
    return uploads, previews


def _invoice(stored_name, mime_type, digest, **extra):
    return Invoice(
        original_name=stored_name,
        stored_name=stored_name,
        mime_type=mime_type,
        file_size=1,
        sha256=digest,
        **extra,
    )


def test_non_pdf_duplicate_is_discarded(session_factory, tmp_path, monkeypatch) -> None:
    uploads, previews = _setup_dirs(tmp_path)
    monkeypatch.setattr(
        mail_service, "get_settings", lambda: SimpleNamespace(upload_dir=uploads, preview_dir=previews)
    )
    with session_factory() as db:
        original = _invoice("a.jpg", "image/jpeg", "1" * 64, status="verified")
        db.add(original)
        db.commit()
        duplicate = _invoice("a-copy.jpg", "image/jpeg", "2" * 64, status="duplicate", duplicate_of=original.id)
        db.add(duplicate)
        db.commit()
        (uploads / "a-copy.jpg").write_bytes(b"x")
        assert mail_service._dedupe_keep_pdf(db, duplicate) is False
        db.expire_all()
        assert db.get(Invoice, duplicate.id) is None
        assert db.get(Invoice, original.id) is not None
        assert not (uploads / "a-copy.jpg").exists()


def test_pdf_duplicate_replaces_non_pdf_original(session_factory, tmp_path, monkeypatch) -> None:
    uploads, previews = _setup_dirs(tmp_path)
    monkeypatch.setattr(
        mail_service, "get_settings", lambda: SimpleNamespace(upload_dir=uploads, preview_dir=previews)
    )
    with session_factory() as db:
        original = _invoice("a.jpg", "image/jpeg", "1" * 64, status="verified")
        db.add(original)
        db.commit()
        (uploads / "a.jpg").write_bytes(b"jpeg")
        (previews / f"{original.id}.jpg").write_bytes(b"preview")
        duplicate = _invoice(
            "a.pdf",
            "application/pdf",
            "2" * 64,
            status="duplicate",
            duplicate_of=original.id,
            verified_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(duplicate)
        db.commit()
        (uploads / "a.pdf").write_bytes(b"%PDF")
        assert mail_service._dedupe_keep_pdf(db, duplicate) is True
        db.expire_all()
        assert db.get(Invoice, original.id) is None
        kept = db.get(Invoice, duplicate.id)
        assert kept is not None
        assert kept.duplicate_of is None
        assert kept.status == "verified"
        assert not (uploads / "a.jpg").exists()
        assert not (previews / f"{original.id}.jpg").exists()


def test_pdf_duplicate_when_original_is_pdf_is_discarded(session_factory, tmp_path, monkeypatch) -> None:
    uploads, previews = _setup_dirs(tmp_path)
    monkeypatch.setattr(
        mail_service, "get_settings", lambda: SimpleNamespace(upload_dir=uploads, preview_dir=previews)
    )
    with session_factory() as db:
        original = _invoice("a.pdf", "application/pdf", "1" * 64, status="verified")
        db.add(original)
        db.commit()
        duplicate = _invoice("a-again.pdf", "application/pdf", "2" * 64, status="duplicate", duplicate_of=original.id)
        db.add(duplicate)
        db.commit()
        (uploads / "a-again.pdf").write_bytes(b"%PDF")
        assert mail_service._dedupe_keep_pdf(db, duplicate) is False
        db.expire_all()
        assert db.get(Invoice, duplicate.id) is None
        assert db.get(Invoice, original.id) is not None
        assert not (uploads / "a-again.pdf").exists()


def test_pdf_dedupe_never_deletes_another_owners_invoice(
    session_factory, tmp_path, monkeypatch
) -> None:
    uploads, previews = _setup_dirs(tmp_path)
    monkeypatch.setattr(
        mail_service,
        "get_settings",
        lambda: SimpleNamespace(upload_dir=uploads, preview_dir=previews),
    )
    with session_factory() as db:
        first_user = User(username="mail-first", email="mail-first@example.com")
        second_user = User(username="mail-second", email="mail-second@example.com")
        db.add_all([first_user, second_user])
        db.flush()
        original = _invoice(
            "other-user.jpg",
            "image/jpeg",
            "4" * 64,
            status="verified",
            owner_id=first_user.id,
        )
        db.add(original)
        db.commit()
        (uploads / original.stored_name).write_bytes(b"jpeg")
        duplicate = _invoice(
            "incoming.pdf",
            "application/pdf",
            "5" * 64,
            status="duplicate",
            duplicate_of=original.id,
            owner_id=second_user.id,
        )
        db.add(duplicate)
        db.commit()
        (uploads / duplicate.stored_name).write_bytes(b"%PDF")

        assert mail_service._dedupe_keep_pdf(db, duplicate) is True
        db.expire_all()
        kept_original = db.get(Invoice, original.id)
        kept_incoming = db.get(Invoice, duplicate.id)
        assert kept_original is not None
        assert kept_incoming is not None
        assert kept_incoming.duplicate_of is None
        assert kept_incoming.status == "review"
        assert (uploads / original.stored_name).exists()
        assert (uploads / duplicate.stored_name).exists()


def test_mail_fetch_limit_keeps_newest_matching_uids():
    values = b" ".join(str(index).encode() for index in range(1, 151))
    selected = mail_service._latest_uid_values([values], 100)
    assert selected[0] == b"51"
    assert selected[-1] == b"150"
    assert len(selected) == 100
