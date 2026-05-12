"""Tests for LangChain agent tools."""
import pytest

pytest.importorskip("chorus_forms", reason="upstream chorus_forms package not installed in public repo")

from chorus_forms.csd.models import CsdForm, FormMeta, FormField
from chorus_csd_analyzer.agent import AgentContext, create_tools, load_knowledge, MAX_ITERATIONS


@pytest.fixture
def context():
    forms = [
        CsdForm(
            meta=FormMeta(fileName="WORK.CSD", formTitle="Work Form", formType="work"),
            fields=[
                FormField(code="ACCT", controlType="input", label="Account", sequence=1, required=True),
                FormField(code="STAT", controlType="combobox", label="Status", sequence=2),
                FormField(code="CRDA", controlType="input", label="Create Date", sequence=3),
            ],
        ),
        CsdForm(
            meta=FormMeta(fileName="CLAIMS.CSD", formTitle="Claims", formType="work"),
            fields=[
                FormField(code="ACCT", controlType="input", label="Account", sequence=1),
                FormField(code="CLMT", controlType="input", label="Claimant", sequence=2),
                FormField(code="STAT", controlType="input", label="Status", sequence=3),
            ],
        ),
    ]
    field_cache = {
        "ACCT": {
            "dataName": "ACCT", "displayName": "Account Number",
            "type": "Alphanumeric", "length": 20, "mask": None,
            "decimals": 0, "groupName": "POLICY", "description": "Policy account number",
        },
        "CRDA": {
            "dataName": "CRDA", "displayName": "Create Date",
            "type": "Date", "length": 10, "mask": "MM/DD/YYYY",
            "decimals": 0, "groupName": None, "description": "Record creation date",
        },
    }
    domain_cache = {
        "STAT": [{"value": "A", "description": "Active"}, {"value": "C", "description": "Closed"}],
    }
    return AgentContext(forms=forms, field_cache=field_cache, domain_cache=domain_cache)


@pytest.fixture
def tools(context):
    return {t.name: t for t in create_tools(context)}


class TestGetFieldDetail:
    def test_found(self, tools):
        result = tools["get_field_detail"].invoke({"field_code": "ACCT"})
        assert "Account Number" in result
        assert "Alphanumeric" in result

    def test_not_found(self, tools):
        result = tools["get_field_detail"].invoke({"field_code": "XXXX"})
        assert "not found" in result.lower()


class TestGetDomainValues:
    def test_found(self, tools):
        result = tools["get_domain_values"].invoke({"field_code": "STAT"})
        assert "Active" in result

    def test_not_found(self, tools):
        result = tools["get_domain_values"].invoke({"field_code": "ACCT"})
        assert "no domain" in result.lower()


class TestListFormFields:
    def test_found(self, tools):
        result = tools["list_form_fields"].invoke({"form_name": "WORK.CSD"})
        assert "ACCT" in result
        assert "STAT" in result

    def test_not_found(self, tools):
        result = tools["list_form_fields"].invoke({"form_name": "NOPE.CSD"})
        assert "not found" in result.lower()


class TestSearchCrossForm:
    def test_shared_field(self, tools):
        result = tools["search_cross_form"].invoke({"field_code": "ACCT"})
        assert "WORK.CSD" in result
        assert "CLAIMS.CSD" in result


class TestGetFormSummary:
    def test_summary(self, tools):
        result = tools["get_form_summary"].invoke({"form_name": "WORK.CSD"})
        assert "Work Form" in result
        assert "3" in result  # field count


class TestCompareForms:
    def test_compare(self, tools):
        result = tools["compare_forms"].invoke({"form_a": "WORK.CSD", "form_b": "CLAIMS.CSD"})
        assert "ACCT" in result  # shared
        assert "STAT" in result  # shared but different types
        assert "CLMT" in result  # only in CLAIMS
        assert "CRDA" in result  # only in WORK

    def test_compare_not_found(self, tools):
        result = tools["compare_forms"].invoke({"form_a": "WORK.CSD", "form_b": "NOPE.CSD"})
        assert "not found" in result.lower()


class TestSuggestFieldTypePromotion:
    def test_suggests_date_and_combobox(self, tools):
        result = tools["suggest_field_type_promotion"].invoke({"form_name": "WORK.CSD"})
        # CRDA is an input but has Date format → suggest date picker
        # STAT is already combobox, no suggestion needed
        # But CLAIMS.CSD has STAT as input with domain values → would suggest combobox
        assert "CRDA" in result

    def test_not_found(self, tools):
        result = tools["suggest_field_type_promotion"].invoke({"form_name": "NOPE.CSD"})
        assert "not found" in result.lower()


class TestLoadKnowledge:
    def test_loads_nonempty(self):
        text = load_knowledge()
        assert len(text) > 100
        assert "AWD" in text
        assert "Field Code" in text


class TestConstants:
    def test_max_iterations(self):
        assert MAX_ITERATIONS == 15
