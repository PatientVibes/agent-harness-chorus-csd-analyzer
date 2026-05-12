"""Tests for Phase 1 enrichment pipeline."""
import pytest
from unittest.mock import AsyncMock

pytest.importorskip("chorus_forms", reason="upstream chorus_forms package not installed in public repo")

from chorus_forms.csd.models import CsdForm, FormMeta, FormField
from chorus_csd_analyzer.enricher import enrich_forms, collect_unique_codes


def _make_form(codes: list[str], form_name: str = "TEST.CSD", control_types: list[str] | None = None) -> CsdForm:
    if control_types is None:
        control_types = ["input"] * len(codes)
    return CsdForm(
        meta=FormMeta(fileName=form_name, formType="work"),
        fields=[FormField(code=c, controlType=ct, sequence=i) for i, (c, ct) in enumerate(zip(codes, control_types))],
    )


class TestCollectUniqueCodes:
    def test_deduplicates(self):
        forms = [_make_form(["ACCT", "STAT"]), _make_form(["ACCT", "NAME"])]
        codes = collect_unique_codes(forms)
        assert sorted(codes) == ["ACCT", "NAME", "STAT"]

    def test_empty_forms(self):
        assert collect_unique_codes([]) == []


class TestEnrichForms:
    @pytest.mark.asyncio
    async def test_attaches_dictionary_info(self):
        form = _make_form(["ACCT", "STAT"])
        mock_client = AsyncMock()
        mock_client.available = True
        mock_client.get_fields_batch.return_value = {
            "ACCT": {
                "dataName": "ACCT", "displayName": "Account Number", "length": 20,
                "type": "Alphanumeric", "mask": None, "decimals": 0,
                "groupName": "POLICY", "description": "Account",
            },
        }
        mock_client.get_domain_values.return_value = None

        enriched, field_cache, domain_cache = await enrich_forms([form], mock_client)
        acct_field = next(f for f in enriched[0].fields if f.code == "ACCT")
        assert acct_field.dictionary is not None
        assert acct_field.dictionary.display_name == "Account Number"
        assert "ACCT" in field_cache

    @pytest.mark.asyncio
    async def test_skips_when_client_unavailable(self):
        form = _make_form(["ACCT"])
        mock_client = AsyncMock()
        mock_client.available = False
        mock_client.get_fields_batch.return_value = {}

        enriched, field_cache, domain_cache = await enrich_forms([form], mock_client)
        assert enriched[0].fields[0].dictionary is None
        assert field_cache == {}

    @pytest.mark.asyncio
    async def test_fetches_domain_values_for_combobox(self):
        form = _make_form(["STAT"], control_types=["combobox"])
        mock_client = AsyncMock()
        mock_client.available = True
        mock_client.get_fields_batch.return_value = {
            "STAT": {"dataName": "STAT", "displayName": "Status", "length": 4,
                     "type": "Alphanumeric", "mask": None, "decimals": 0,
                     "groupName": None, "description": None},
        }
        mock_client.get_domain_values.return_value = [{"value": "A", "description": "Active"}]

        enriched, field_cache, domain_cache = await enrich_forms([form], mock_client)
        assert "STAT" in domain_cache
        stat_field = enriched[0].fields[0]
        assert stat_field.dictionary is not None
        assert stat_field.dictionary.domain_values is not None
