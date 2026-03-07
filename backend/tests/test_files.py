"""TC-FILES: File download endpoint — path traversal protection, type validation."""
import pytest
from pathlib import Path
from app.config import settings


def _create_test_file(filename: str, content: bytes = b"test content"):
    generated_dir = Path(settings.UPLOAD_DIR) / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    path = generated_dir / filename
    path.write_bytes(content)
    return path


def test_download_valid_html_file(client):
    path = _create_test_file("test_output.html", b"<html><body>OK</body></html>")
    try:
        resp = client.get("/api/files/test_output.html")
        assert resp.status_code == 200
        assert b"OK" in resp.content
    finally:
        path.unlink(missing_ok=True)


def test_download_disallowed_extension_rejected(client):
    resp = client.get("/api/files/evil.py")
    assert resp.status_code == 400


def test_path_traversal_rejected(client):
    resp = client.get("/api/files/../../etc/passwd")
    assert resp.status_code in (400, 404, 422)


def test_path_traversal_with_slash_rejected(client):
    resp = client.get("/api/files/subdir/file.html")
    assert resp.status_code in (400, 404, 422)


def test_nonexistent_file_404(client):
    resp = client.get("/api/files/nonexistent_file.pdf")
    assert resp.status_code == 404


def test_double_dot_in_filename_rejected(client):
    resp = client.get("/api/files/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404, 422)
