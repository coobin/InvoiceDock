import io
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import User
from app.services import ingestion, quota_service
from app.services.ingestion import (
    detect_mime,
    extract_zip_candidates,
    ingest_bytes,
    safe_filename,
    validate_file,
)


def test_filename_is_reduced_to_basename():
    assert safe_filename("../../危险 发票?.pdf") == "危险 发票_.pdf"


def test_magic_bytes_are_checked():
    assert detect_mime(b"%PDF-1.7\n", "invoice.bin") == "application/pdf"
    with pytest.raises(ValueError, match="文件内容"):
        validate_file(b"not a pdf", "invoice.pdf")


def test_zip_only_returns_supported_candidates():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("invoice.pdf", b"%PDF-1.4\n")
        archive.writestr("notes.exe", b"ignored")
    results = extract_zip_candidates(buffer.getvalue())
    assert results == [("invoice.pdf", b"%PDF-1.4\n")]


def test_zip_file_count_is_bounded():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(3):
            archive.writestr(f"{index}.pdf", b"%PDF")
    with pytest.raises(ValueError, match="文件数"):
        extract_zip_candidates(buffer.getvalue(), max_files=2)


def test_zip_rejects_oversized_candidate(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        max_upload_mb=1,
        max_archive_files=10,
        max_archive_uncompressed_mb=10,
    )
    monkeypatch.setattr(ingestion, "get_settings", lambda: settings)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("too-large.pdf", b"%PDF" + b"x" * (1024 * 1024))
    with pytest.raises(ValueError, match="压缩包"):
        extract_zip_candidates(buffer.getvalue())


def test_sha256_deduplication_is_scoped_to_owner(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    fake_settings = SimpleNamespace(
        max_upload_mb=20,
        upload_dir=upload_dir,
        max_user_storage_mb=100,
        max_user_daily_upload_files=20,
        tz="Asia/Shanghai",
    )
    monkeypatch.setattr(
        ingestion,
        "get_settings",
        lambda: fake_settings,
    )
    monkeypatch.setattr(quota_service, "get_settings", lambda: fake_settings)
    engine = create_engine(f"sqlite:///{tmp_path / 'owners.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    with session_factory() as db:
        first_user = User(username="first", email="first@example.com")
        second_user = User(username="second", email="second@example.com")
        db.add_all([first_user, second_user])
        db.commit()

        first, first_created = ingest_bytes(
            db, b"%PDF-1.4\nshared", "shared.pdf", owner_id=first_user.id
        )
        same_owner, same_owner_created = ingest_bytes(
            db, b"%PDF-1.4\nshared", "copy.pdf", owner_id=first_user.id
        )
        second, second_created = ingest_bytes(
            db, b"%PDF-1.4\nshared", "shared.pdf", owner_id=second_user.id
        )

        assert first_created is True
        assert same_owner_created is False
        assert same_owner.id == first.id
        assert second_created is True
        assert second.id != first.id
        assert second.owner_id == second_user.id
