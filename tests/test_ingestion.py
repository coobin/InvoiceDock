import io
import zipfile

import pytest

from app.services.ingestion import detect_mime, extract_zip_candidates, safe_filename, validate_file


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

