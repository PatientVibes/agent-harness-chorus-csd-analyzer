"""Tests for the conversion pipeline."""
import zipfile
import io
import json
import pytest
from pathlib import Path

pytest.importorskip("chorus_forms", reason="upstream chorus_forms package not installed in public repo")


@pytest.fixture
def work_csd(csd_fixtures_dir):
    path = csd_fixtures_dir / "WORK.CSD"
    if not path.exists():
        pytest.skip("WORK.CSD fixture not available")
    return path

@pytest.fixture
def wells_csd(csd_fixtures_dir):
    path = csd_fixtures_dir / "WELLS.CSD"
    if not path.exists():
        pytest.skip("WELLS.CSD fixture not available")
    return path

class TestConvertFiles:
    @pytest.mark.integration
    def test_single_file_returns_zip_bytes(self, work_csd, tmp_path):
        from chorus_csd_analyzer.converter import convert_files
        result = convert_files([work_csd], tmp_path)
        assert isinstance(result, bytes)
        assert len(result) > 0

    @pytest.mark.integration
    def test_zip_contains_xml_and_json(self, work_csd, tmp_path):
        from chorus_csd_analyzer.converter import convert_files
        result = convert_files([work_csd], tmp_path)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
            assert any(n.startswith("xml/") and n.endswith(".xml") for n in names)
            assert any(n.startswith("json/") and n.endswith(".json") for n in names)
            assert "_manifest.json" in names

    @pytest.mark.integration
    def test_multiple_files(self, work_csd, wells_csd, tmp_path):
        from chorus_csd_analyzer.converter import convert_files
        result = convert_files([work_csd, wells_csd], tmp_path)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            xml_files = [n for n in zf.namelist() if n.startswith("xml/") and n.endswith(".xml")]
            assert len(xml_files) == 2

    def test_invalid_file_included_as_error(self, tmp_path):
        from chorus_csd_analyzer.converter import convert_files
        bad_file = tmp_path / "BAD.CSD"
        bad_file.write_bytes(b"\x00" * 10)
        result = convert_files([bad_file], tmp_path / "out")
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            manifest = json.loads(zf.read("_manifest.json"))
            assert manifest["totalErrors"] > 0

    @pytest.mark.integration
    def test_manifest_has_expected_fields(self, work_csd, tmp_path):
        from chorus_csd_analyzer.converter import convert_files
        result = convert_files([work_csd], tmp_path)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            manifest = json.loads(zf.read("_manifest.json"))
            assert "totalForms" in manifest
            assert "totalErrors" in manifest
            assert "forms" in manifest
            assert manifest["totalForms"] == 1
