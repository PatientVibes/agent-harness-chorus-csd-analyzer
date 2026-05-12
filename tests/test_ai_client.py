"""Tests for AI Gateway client."""
import json
import pytest

pytest.importorskip("chorus_forms", reason="upstream chorus_forms package not installed in public repo")

from chorus_csd_analyzer.ai_client import AIGatewayClient, extract_json
from chorus_forms.csd.models import CsdForm, FormMeta, FormField


class TestExtractJson:
    def test_raw_json(self):
        result = extract_json('{"rules": []}')
        assert result == {"rules": []}

    def test_fenced_json(self):
        text = 'Here is the analysis:\n```json\n{"rules": ["required"]}\n```\nDone.'
        result = extract_json(text)
        assert result == {"rules": ["required"]}

    def test_fenced_no_lang(self):
        text = '```\n{"rules": []}\n```'
        result = extract_json(text)
        assert result == {"rules": []}

    def test_embedded_json(self):
        text = 'The result is {"rules": ["numeric"]} as expected.'
        result = extract_json(text)
        assert result == {"rules": ["numeric"]}

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Could not extract"):
            extract_json("no json here")


class TestAIGatewayClient:
    def test_init_without_url_is_unavailable(self):
        client = AIGatewayClient(url=None, api_key=None)
        assert client.available is False

    def test_init_with_url_sets_available(self):
        client = AIGatewayClient(url="https://example.com", api_key="key")
        assert client.available is True

    def test_build_messages_structure(self):
        form = CsdForm(
            meta=FormMeta(fileName="TEST.CSD", formTitle="Test", formType="work"),
            fields=[
                FormField(code="ACCT", controlType="input", label="Account", sequence=1),
                FormField(code="STAT", controlType="input", label="Status", sequence=2),
            ],
        )
        client = AIGatewayClient(url="https://example.com", api_key="key")
        messages = client.build_messages(form)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "TEST.CSD" in messages[1]["content"]
        assert "ACCT" in messages[1]["content"]
