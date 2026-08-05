from __future__ import annotations

from types import SimpleNamespace

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
