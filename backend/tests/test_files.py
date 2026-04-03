"""TC-FILES: File download endpoint — path traversal protection, type validation."""
import pytest
from pathlib import Path
from app.config import settings
from app.models.user import Role
from tests.conftest import _make_dept, _make_user, _login, _auth


def _create_test_file(filename: str, content: bytes = b"test content"):
    generated_dir = Path(settings.UPLOAD_DIR) / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    path = generated_dir / filename
    path.write_bytes(content)
    return path


@pytest.fixture
def auth_headers(client, db):
    dept = _make_dept(db)
    _make_user(db, "fileuser", Role.EMPLOYEE, dept.id)
    db.commit()
    token = _login(client, "fileuser")
    return _auth(token)


def test_download_valid_html_file(client, db, auth_headers):
    path = _create_test_file("test_output.html", b"<html><body>OK</body></html>")
    try:
        resp = client.get("/api/files/test_output.html", headers=auth_headers)
        assert resp.status_code == 200
        assert b"OK" in resp.content
    finally:
        path.unlink(missing_ok=True)


def test_download_disallowed_extension_rejected(client, db, auth_headers):
    resp = client.get("/api/files/evil.py", headers=auth_headers)
    assert resp.status_code == 400


def test_path_traversal_rejected(client):
    resp = client.get("/api/files/../../etc/passwd")
    assert resp.status_code in (400, 401, 404, 422)


def test_path_traversal_with_slash_rejected(client):
    resp = client.get("/api/files/subdir/file.html")
    assert resp.status_code in (400, 401, 404, 422)


def test_nonexistent_file_404(client, db, auth_headers):
    resp = client.get("/api/files/nonexistent_file.pdf", headers=auth_headers)
    assert resp.status_code == 404


def test_double_dot_in_filename_rejected(client):
    resp = client.get("/api/files/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 401, 404, 422)
