"""Integration tests for FastAPI endpoints."""
import io
import json
import zipfile
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

pytest.importorskip("chorus_forms", reason="upstream chorus_forms package not installed in public repo")


@pytest.fixture
def client():
    from chorus_csd_analyzer.app import app
    return TestClient(app)

@pytest.fixture
def work_csd_bytes(csd_fixtures_dir):
    path = csd_fixtures_dir / "WORK.CSD"
    if not path.exists():
        pytest.skip("WORK.CSD fixture not available")
    return path.read_bytes()

class TestIndex:
    def test_get_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "CSD Converter" in resp.text
        assert "text/html" in resp.headers["content-type"]

class TestUpload:
    @pytest.mark.integration
    def test_upload_valid_file(self, client, work_csd_bytes):
        resp = client.post("/upload", files=[("files", ("WORK.CSD", work_csd_bytes, "application/octet-stream"))])
        assert resp.status_code == 200
        assert "WORK.CSD" in resp.text

    def test_upload_invalid_extension(self, client):
        resp = client.post("/upload", files=[("files", ("readme.txt", b"hello", "text/plain"))])
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower() or "Invalid" in resp.text

    @pytest.mark.integration
    def test_upload_multiple_files(self, client, work_csd_bytes):
        resp = client.post("/upload", files=[
            ("files", ("WORK.CSD", work_csd_bytes, "application/octet-stream")),
            ("files", ("COPY.CSD", work_csd_bytes, "application/octet-stream")),
        ])
        assert resp.status_code == 200
        assert "WORK.CSD" in resp.text
        assert "COPY.CSD" in resp.text

class TestConvert:
    @pytest.mark.integration
    def test_convert_returns_zip(self, client, work_csd_bytes):
        client.post("/upload", files=[("files", ("WORK.CSD", work_csd_bytes, "application/octet-stream"))])
        resp = client.post("/convert")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert any(n.endswith(".xml") for n in names)
        assert "_manifest.json" in names

    def test_convert_with_no_files(self, client):
        resp = client.post("/convert")
        assert resp.status_code in (200, 400)

class TestStatus:
    def test_status_no_gateway(self, client):
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "ai_available" in data
        assert "chorus_available" in data
