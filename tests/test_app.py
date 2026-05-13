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


class TestAIGatewayModelEnvVar:
    """Verify AI_GATEWAY_MODEL env var reaches analyze_forms (G6a)."""

    def test_env_var_passed_to_analyze_forms(self, monkeypatch):
        """AI_GATEWAY_MODEL env var must be threaded through to analyze_forms."""
        from unittest.mock import patch

        monkeypatch.setenv("AI_GATEWAY_MODEL", "Qwen/Qwen3-32B-test")
        monkeypatch.setenv("AI_GATEWAY_URL", "https://example.invalid")
        monkeypatch.setenv("AI_GATEWAY_KEY", "test-key")

        # Re-import app.py so it picks up the env vars at module load time.
        import importlib
        import chorus_csd_analyzer.app as app_module
        importlib.reload(app_module)

        # Sanity check: the module-level constant reflects the env var.
        assert app_module.AI_GATEWAY_MODEL == "Qwen/Qwen3-32B-test"

        captured = {}

        async def fake_analyze_forms(*args, **kwargs):
            captured["model"] = kwargs.get("model")
            return {"forms": {}, "crossFormReport": {}}

        # Stub parse_csd_file so /convert reaches analyze_forms with non-empty
        # parsed_forms (a fake .csd byte payload would otherwise raise during
        # parsing and short-circuit the route). The fake provides only the
        # surface analyze_forms inspects upstream of the LLM call.
        class _FakeMeta:
            file_name = "FAKE.CSD"
            dll_hooks: list[str] = []
        class _FakeForm:
            meta = _FakeMeta()
            fields: list = []

        # An empty-but-valid ZIP central-directory record so convert_files'
        # mock return is a valid bytes payload.
        empty_zip = b"PK\x05\x06" + b"\x00" * 18

        with (
            patch("chorus_csd_analyzer.app.analyze_forms", new=fake_analyze_forms),
            patch("chorus_csd_analyzer.app.parse_csd_file", return_value=_FakeForm()),
            patch("chorus_csd_analyzer.app.convert_files", return_value=empty_zip),
        ):
            from fastapi.testclient import TestClient
            client = TestClient(app_module.app)

            files = [("files", ("smoke.csd", b"fake-csd-bytes", "application/octet-stream"))]
            upload_resp = client.post("/upload", files=files)
            assert upload_resp.status_code == 200

            # TestClient persists cookies across requests on the same instance,
            # so /convert sees the csd_session cookie set by /upload.
            convert_resp = client.post(
                "/convert",
                data={"use_ai": "true", "use_enrich": "false"},
            )
            # The /convert response shape isn't the assertion target — we care
            # that analyze_forms received the env-var model. But it must not
            # have 5xx'd; that would mean the route never reached analyze_forms.
            assert convert_resp.status_code < 500, (
                f"/convert returned {convert_resp.status_code}; "
                f"the route likely failed before reaching analyze_forms."
            )

        assert captured.get("model") == "Qwen/Qwen3-32B-test", (
            f"AI_GATEWAY_MODEL not threaded through to analyze_forms; "
            f"got model={captured.get('model')!r}"
        )
