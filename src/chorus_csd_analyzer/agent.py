"""LangChain agent with LangGraph ReAct loop for CSD form analysis.

Uses Qwen3-30B-A3B via the AI Gateway with LangGraph's create_react_agent.
The agent decides which tools to invoke based on the forms it's analyzing.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from chorus_forms.csd.models import CsdForm
from token_tracker import TokenTracker
from llm_utils import load_checkpoint, retry_async, sanitize_field_text, save_checkpoint

logger = logging.getLogger(__name__)

# Kept for documentation and backward-compatibility (manual loop used this limit).
MAX_ITERATIONS = 15

DEFAULT_MODEL = "Qwen/Qwen3-30B-A3B"
KNOWLEDGE_PATH = Path(__file__).parent / "knowledge" / "awd_reference.md"


# ---------------------------------------------------------------------------
# Pydantic input schemas for tools
# ---------------------------------------------------------------------------


class FieldCodeInput(BaseModel):
    """Input schema for field-code-based tools."""
    field_code: str = Field(description="The AWD field code to look up (e.g., 'ACCT', 'STAT')")


class FormNameInput(BaseModel):
    """Input schema for form-name-based tools."""
    form_name: str = Field(description="The form file name (e.g., 'WORK.CSD')")


class CompareFormsInput(BaseModel):
    """Input schema for form comparison."""
    form_a: str = Field(description="First form file name")
    form_b: str = Field(description="Second form file name")


# ---------------------------------------------------------------------------
# Structured output models
# ---------------------------------------------------------------------------


class FieldAnalysisItem(BaseModel):
    field: str = ""
    inferredType: str = ""
    rules: list[str] = Field(default_factory=list)
    notes: str = ""


class DllHookItem(BaseModel):
    hook: str = ""
    interpretation: str = ""


class FormAnalysis(BaseModel):
    businessPurpose: str = ""
    classification: str = ""
    fieldAnalysis: list[FieldAnalysisItem] = Field(default_factory=list)
    riskAreas: list[str] = Field(default_factory=list)
    dllHookInterpretation: list[DllHookItem] = Field(default_factory=list)
    typePromotions: list[str] = Field(default_factory=list)


class DuplicateReport(BaseModel):
    forms: list[str] = Field(default_factory=list)
    similarity: str = ""
    reason: str = ""


class InconsistencyReport(BaseModel):
    field: str = ""
    issue: str = ""


class CrossFormReport(BaseModel):
    classifications: dict[str, list[str]] = Field(default_factory=dict)
    duplicates: list[DuplicateReport] = Field(default_factory=list)
    inconsistencies: list[InconsistencyReport] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class FormAnalysisResult(BaseModel):
    forms: dict[str, FormAnalysis] = Field(default_factory=dict)
    crossFormReport: CrossFormReport = Field(default_factory=CrossFormReport)


# ---------------------------------------------------------------------------
# Agent context and helpers
# ---------------------------------------------------------------------------


@dataclass
class AgentContext:
    """Shared context for agent tools — populated by Phase 1 enrichment."""

    forms: list[CsdForm] = field(default_factory=list)
    field_cache: dict[str, dict] = field(default_factory=dict)
    domain_cache: dict[str, list] = field(default_factory=dict)


def load_knowledge() -> str:
    """Load the AWD domain knowledge markdown file."""
    if KNOWLEDGE_PATH.exists():
        return KNOWLEDGE_PATH.read_text(encoding="utf-8")
    logger.warning("Knowledge file not found: %s", KNOWLEDGE_PATH)
    return ""


def create_tools(ctx: AgentContext) -> list:
    """Create LangChain tools as closures over the given context.

    The LLM only sees simple string parameters (field_code, form_name).
    The context is captured in the closure.
    """

    @tool(args_schema=FieldCodeInput)
    def get_field_detail(field_code: str) -> str:
        """Look up a field's definition: display name, format type, mask, length, group, and description."""
        info = ctx.field_cache.get(field_code)
        if not info:
            return f"Field '{field_code}' not found in Chorus field definitions."
        parts = [f"Field: {field_code}"]
        for key, label in [
            ("displayName", "Display Name"),
            ("type", "Type"),
            ("length", "Length"),
            ("mask", "Mask"),
            ("decimals", "Decimals"),
            ("groupName", "Group"),
            ("description", "Description"),
        ]:
            val = info.get(key)
            if val is not None:
                parts.append(f"  {label}: {sanitize_field_text(str(val))}")
        return "\n".join(parts)

    @tool(args_schema=FieldCodeInput)
    def get_domain_values(field_code: str) -> str:
        """Get the list of valid dropdown/combobox values for a field."""
        values = ctx.domain_cache.get(field_code)
        if not values:
            return f"No domain values found for field '{field_code}'."
        lines = [f"Domain values for {field_code} ({len(values)} values):"]
        for v in values[:50]:
            desc = sanitize_field_text(v.get("description", ""))
            val = sanitize_field_text(v.get("value", "?"))
            lines.append(f"  {val}: {desc}" if desc else f"  {val}")
        if len(values) > 50:
            lines.append(f"  ... and {len(values) - 50} more")
        return "\n".join(lines)

    @tool(args_schema=FormNameInput)
    def list_form_fields(form_name: str) -> str:
        """List all fields in a form with their control type, label, and enrichment data."""
        form = next((f for f in ctx.forms if f.meta.file_name == form_name), None)
        if not form:
            return f"Form '{form_name}' not found."
        total = len(form.fields)
        # Cap output to prevent context bloat on large forms
        max_display = 25
        display_fields = form.fields[:max_display]
        lines = [f"Fields in {form_name} ({total} fields):"]
        for f in display_fields:
            parts = [f"  {f.code}"]
            if f.label:
                parts.append(f'label="{sanitize_field_text(f.label)}"')
            parts.append(f"type={f.control_type}")
            if f.required:
                parts.append("REQUIRED")
            if f.read_only:
                parts.append("READ-ONLY")
            if f.dictionary and f.dictionary.display_name:
                parts.append(f"displayName={sanitize_field_text(f.dictionary.display_name)}")
            if f.dictionary and f.dictionary.type:
                parts.append(f"format={f.dictionary.type}")
            lines.append(" ".join(parts))
        if total > max_display:
            remaining = form.fields[max_display:]
            lines.append(f"  ... and {len(remaining)} more fields: {', '.join(f.code for f in remaining)}")
        return "\n".join(lines)

    @tool(args_schema=FieldCodeInput)
    def search_cross_form(field_code: str) -> str:
        """Find which forms use a given field code and what control type each uses."""
        matches = []
        for form in ctx.forms:
            for f in form.fields:
                if f.code == field_code:
                    note = f"{form.meta.file_name}: {f.control_type}"
                    if f.required:
                        note += " (required)"
                    matches.append(note)
                    break
        if not matches:
            return f"Field '{field_code}' not found in any form."
        return f"Field '{field_code}' appears in {len(matches)} form(s):\n" + "\n".join(f"  {m}" for m in matches)

    @tool(args_schema=FormNameInput)
    def get_form_summary(form_name: str) -> str:
        """Get form metadata: title, type, page count, field count, groups, DLL hooks."""
        form = next((f for f in ctx.forms if f.meta.file_name == form_name), None)
        if not form:
            return f"Form '{form_name}' not found."
        lines = [
            f"Form: {form.meta.file_name}",
            f"  Title: {sanitize_field_text(form.meta.form_title or 'Unknown')}",
            f"  Type: {form.meta.form_type}",
            f"  Pages: {form.meta.num_pages}",
            f"  Fields: {len(form.fields)}",
            f"  Groups: {len(form.groups)}",
        ]
        if form.meta.dll_hooks:
            lines.append(f"  DLL Hooks: {', '.join(sanitize_field_text(h) for h in form.meta.dll_hooks)}")
        if form.warnings:
            lines.append(f"  Parse Warnings: {len(form.warnings)}")
        return "\n".join(lines)

    @tool(args_schema=CompareFormsInput)
    def compare_forms(form_a: str, form_b: str) -> str:
        """Compare two forms side-by-side: shared fields, unique fields, and control type differences."""
        fa = next((f for f in ctx.forms if f.meta.file_name == form_a), None)
        fb = next((f for f in ctx.forms if f.meta.file_name == form_b), None)
        if not fa:
            return f"Form '{form_a}' not found."
        if not fb:
            return f"Form '{form_b}' not found."

        fields_a = {f.code: f for f in fa.fields}
        fields_b = {f.code: f for f in fb.fields}
        codes_a = set(fields_a.keys())
        codes_b = set(fields_b.keys())

        shared = sorted(codes_a & codes_b)
        only_a = sorted(codes_a - codes_b)
        only_b = sorted(codes_b - codes_a)

        lines = [f"Comparison: {form_a} vs {form_b}"]

        if shared:
            lines.append(f"\nShared fields ({len(shared)}):")
            for code in shared:
                fa_type = fields_a[code].control_type
                fb_type = fields_b[code].control_type
                if fa_type != fb_type:
                    lines.append(f"  {code}: {fa_type} vs {fb_type} *** DIFFERENT ***")
                else:
                    lines.append(f"  {code}: {fa_type}")

        if only_a:
            lines.append(f"\nOnly in {form_a} ({len(only_a)}):")
            for code in only_a:
                lines.append(f"  {code}: {fields_a[code].control_type}")

        if only_b:
            lines.append(f"\nOnly in {form_b} ({len(only_b)}):")
            for code in only_b:
                lines.append(f"  {code}: {fields_b[code].control_type}")

        return "\n".join(lines)

    @tool(args_schema=FormNameInput)
    def suggest_field_type_promotion(form_name: str) -> str:
        """Suggest fields that should be promoted from text input to a richer control type based on their metadata."""
        form = next((f for f in ctx.forms if f.meta.file_name == form_name), None)
        if not form:
            return f"Form '{form_name}' not found."

        suggestions = []
        for f in form.fields:
            if f.control_type != "input":
                continue  # already a rich control

            # Check if field has domain values → should be combobox
            if f.code in ctx.domain_cache:
                values = ctx.domain_cache[f.code]
                suggestions.append(
                    f"  {f.code}: input → combobox ({len(values)} domain values available)"
                )
                continue

            # Check if field has Date format → should be date picker
            if f.dictionary and f.dictionary.type and "date" in f.dictionary.type.lower():
                suggestions.append(
                    f"  {f.code}: input → datePicker (format: {f.dictionary.type})"
                )
                continue

            # Check code patterns for dates
            if f.code in {"CRDA", "RCDA", "PRDA", "STDA", "DODA", "EFDA", "EXDA", "CLDA"}:
                suggestions.append(
                    f"  {f.code}: input → datePicker (known date field code)"
                )

        if not suggestions:
            return f"No field type promotions suggested for {form_name}."

        return f"Suggested promotions for {form_name}:\n" + "\n".join(suggestions)

    return [
        get_field_detail,
        get_domain_values,
        list_form_fields,
        search_cross_form,
        get_form_summary,
        compare_forms,
        suggest_field_type_promotion,
    ]


def _fallback_parse(text: str, forms: list[CsdForm]) -> dict:
    """Build a minimal FormAnalysisResult dict from plain-text agent output.

    Used when structured extraction hits a token limit. Extracts one paragraph
    per form as the businessPurpose.
    """
    import re
    result: dict = {"forms": {}, "crossFormReport": {"classifications": {}, "duplicates": [], "inconsistencies": [], "recommendations": []}}
    for form in forms:
        stem = form.meta.file_name.replace(".CSD", "").replace(".LKP", "")
        # Try to find a paragraph that mentions this form
        pattern = re.compile(rf"{re.escape(stem)}[:\s]+(.*?)(?=\n\n|\Z)", re.DOTALL | re.IGNORECASE)
        m = pattern.search(text)
        purpose = m.group(1).strip()[:500] if m else f"Analysis available in agent text (extraction failed). Form: {stem}"
        result["forms"][stem] = {
            "businessPurpose": purpose,
            "classification": "",
            "fieldAnalysis": [],
            "riskAreas": [],
            "dllHookInterpretation": [],
            "typePromotions": [],
        }
    return result


SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"


def _load_system_prompt_template() -> str:
    """Load the system prompt template from the vendored skill file.

    The template contains a `{knowledge}` placeholder filled at runtime via
    .format(knowledge=load_knowledge()).
    """
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


SYSTEM_PROMPT_TEMPLATE = _load_system_prompt_template()


def _verify_analysis(analysis: FormAnalysis, form: CsdForm) -> list[str]:
    """Verify a FormAnalysis against the source form data.

    Returns a list of issues found. Empty list means verification passed.
    """
    issues = []
    field_codes = {f.code for f in form.fields}
    analyzed_codes = {item.field for item in analysis.fieldAnalysis}

    # Check: how many fields were actually analyzed (not backfilled stubs)?
    non_stub = [
        item for item in analysis.fieldAnalysis
        if item.notes and "requires client field registry" not in item.notes
    ]
    coverage = len(non_stub) / len(form.fields) if form.fields else 1.0
    if coverage < 0.5:
        issues.append(
            f"Low field coverage: only {len(non_stub)}/{len(form.fields)} fields "
            f"have substantive analysis (need at least 50%)"
        )

    # Check: type promotions reference real field codes
    for promo in analysis.typePromotions:
        promo_codes = [c for c in field_codes if c in promo]
        if not promo_codes:
            issues.append(f"Type promotion references unknown field: {promo[:80]}")

    # Check: DLL hooks match actual hooks from the form
    actual_hooks = set(form.meta.dll_hooks)
    if not actual_hooks and analysis.dllHookInterpretation:
        issues.append(
            f"Analysis lists {len(analysis.dllHookInterpretation)} DLL hooks "
            f"but form has none"
        )

    # Check: risk areas don't reference non-existent field codes
    for risk in analysis.riskAreas:
        # Look for 4-char uppercase codes in the risk text
        import re
        codes_in_risk = re.findall(r'\b[A-Z][A-Z0-9#]{2,3}\b', risk)
        for code in codes_in_risk:
            if code not in field_codes and code not in {
                "AWD", "CSD", "LOB", "DLL", "API", "XML", "TIN", "SSN",
            }:
                issues.append(f"Risk area references unknown code '{code}': {risk[:60]}")
                break

    return issues


async def _analyze_single_form(
    form: CsdForm,
    ctx: AgentContext,
    llm: ChatOpenAI,
    gateway_url: str,
    gateway_key: str,
    model: str,
    tracker: Optional[TokenTracker],
) -> FormAnalysis:
    """Run one ReAct + structured-extraction cycle for a single CSD form."""
    tools = create_tools(ctx)
    agent = create_react_agent(model=llm, tools=tools, prompt=SYSTEM_PROMPT_TEMPLATE.format(knowledge=load_knowledge()))

    stem = form.meta.file_name
    field_count = len(form.fields)
    user_prompt = (
        f"Analyze this single CSD form: {stem}\n"
        f"Form type: {getattr(form.meta, 'form_type', 'unknown')}, "
        f"{field_count} fields.\n\n"
        "Use list_form_fields, suggest_field_type_promotion, and get_field_detail "
        "to investigate. Then summarize:\n"
        "- Business purpose and classification\n"
        f"- Field analysis for EVERY field (all {field_count} — do not skip any)\n"
        "- Type promotions with specific reasons\n"
        "- DLL hook interpretations (call get_form_summary to check)\n"
        "- Risk areas grounded in actual field data"
    )

    final_state: dict = {}
    all_messages: list = []

    try:
        async for chunk in agent.astream(
            {"messages": [HumanMessage(content=user_prompt)]},
            config={"recursion_limit": 20},
        ):
            final_state = chunk
            for node_output in chunk.values():
                if isinstance(node_output, dict):
                    for msg in node_output.get("messages", []):
                        all_messages.append(msg)
    except Exception as e:
        logger.warning("ReAct loop failed for %s: %s", stem, e)

    # Track tokens from ReAct loop
    if tracker is not None:
        react_in = react_out = 0
        for msg in all_messages:
            um = getattr(msg, "usage_metadata", None)
            if um:
                react_in += um.get("input_tokens", 0)
                react_out += um.get("output_tokens", 0)
        if react_in or react_out:
            tracker.record(source="agent_react", ref=stem, model=model,
                           input_tokens=react_in, output_tokens=react_out)

    # Get final agent message
    final_message = None
    if "agent" in final_state:
        msgs = final_state["agent"].get("messages", [])
        if msgs:
            final_message = msgs[-1]

    if final_message is None:
        return FormAnalysis(businessPurpose=f"Analysis failed for {stem}")

    # Strip Qwen3 <think>...</think> blocks from the final message to avoid
    # bloating the structured extraction context with reasoning traces.
    raw_content = getattr(final_message, "content", "") or ""
    cleaned = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    if cleaned != raw_content:
        from langchain_core.messages import AIMessage
        final_message = AIMessage(content=cleaned)

    # Structured extraction for this single form — much smaller output
    extraction_llm = ChatOpenAI(
        base_url=f"{gateway_url.rstrip('/')}/v1",
        api_key=gateway_key,
        model=model,
        temperature=0.0,
        max_tokens=8192,
        default_headers={"X-API-Key": gateway_key},
    )
    structured_llm = extraction_llm.with_structured_output(FormAnalysis)
    t0 = time.monotonic()
    try:
        extraction_messages = [
            SystemMessage(content=(
                f"Extract the analysis into structured JSON for this single form. "
                f"The form has {len(form.fields)} fields — include an entry in fieldAnalysis "
                f"for EVERY field (exactly {len(form.fields)} entries expected). "
                f"Do NOT limit to 5 or any subset. "
                f"If the agent found DLL hooks, populate dllHookInterpretation with each hook name "
                f"and its interpretation. Keep each field's notes to one sentence."
            )),
            final_message,
        ]
        result = await retry_async(
            lambda: structured_llm.ainvoke(extraction_messages)
        )
        latency_ms = (time.monotonic() - t0) * 1000

        if tracker is not None:
            um = getattr(result, "usage_metadata", None)
            if um:
                tracker.record(source="agent_structured", ref=stem, model=model,
                               input_tokens=um.get("input_tokens", 0),
                               output_tokens=um.get("output_tokens", 0),
                               latency_ms=latency_ms)

        # with_structured_output may return FormAnalysis, a dict, or a wrapper
        if isinstance(result, FormAnalysis):
            analysis = result
        elif isinstance(result, dict):
            analysis = FormAnalysis(**result)
        else:
            parsed = getattr(result, "parsed", result)
            if isinstance(parsed, FormAnalysis):
                analysis = parsed
            elif isinstance(parsed, dict):
                analysis = FormAnalysis(**parsed)
            else:
                logger.warning("Structured extraction for %s returned %s", stem, type(result).__name__)
                text = getattr(final_message, "content", "") or ""
                return FormAnalysis(businessPurpose=text[:400])

        # Verify extraction quality and retry once if issues found
        issues = _verify_analysis(analysis, form)
        if issues:
            logger.info("Verification found %d issues for %s, retrying extraction", len(issues), stem)
            issue_text = "\n".join(f"- {i}" for i in issues)
            try:
                retry_result = await structured_llm.ainvoke([
                    SystemMessage(content=(
                        f"The previous extraction had quality issues. Fix them:\n"
                        f"{issue_text}\n\n"
                        f"Re-extract the analysis for this form with {len(form.fields)} fields. "
                        f"Include an entry in fieldAnalysis for EVERY field. "
                        f"Only reference field codes that exist in the form. "
                        f"Only list DLL hooks if the form actually has them."
                    )),
                    final_message,
                ])
                if isinstance(retry_result, FormAnalysis):
                    analysis = retry_result
                elif isinstance(retry_result, dict):
                    analysis = FormAnalysis(**retry_result)
                else:
                    parsed = getattr(retry_result, "parsed", retry_result)
                    if isinstance(parsed, FormAnalysis):
                        analysis = parsed
                    elif isinstance(parsed, dict):
                        analysis = FormAnalysis(**parsed)

                if tracker is not None:
                    um = getattr(retry_result, "usage_metadata", None)
                    if um:
                        tracker.record(source="agent_structured", ref=f"{stem}_retry",
                                       model=model,
                                       input_tokens=um.get("input_tokens", 0),
                                       output_tokens=um.get("output_tokens", 0))
            except Exception as retry_err:
                logger.warning("Verification retry failed for %s: %s", stem, retry_err)

        # Backfill: ensure every field from the form appears in fieldAnalysis.
        # The extraction LLM often self-limits to ~5 entries; pad with stubs
        # so the output is comprehensive even if notes are sparse.
        from chorus_forms.csd.adapter import CHORUS_SYSTEM_FIELDS
        _SYSTEM_NOTES = {
            "UNIT": "Chorus system field — Business Area (always read-only)",
            "WRKT": "Chorus system field — Work Type (always combobox)",
            "STAT": "Chorus system field — Status (always combobox)",
            "AMTV": "Chorus system field — Amount Value (always numeric, 2 decimals)",
            "AMTT": "Chorus system field — Amount Type (always S=Share / D=Dollars)",
        }
        analyzed_codes = {item.field for item in analysis.fieldAnalysis}
        for fld in form.fields:
            code = getattr(fld, "code", None) or getattr(fld, "field_code", None)
            if code and code not in analyzed_codes:
                if code in CHORUS_SYSTEM_FIELDS:
                    notes = _SYSTEM_NOTES.get(code, "Chorus system field")
                    inferred = "system"
                else:
                    notes = "LOB field — requires client field registry for accurate typing"
                    inferred = "string"
                analysis.fieldAnalysis.append(FieldAnalysisItem(
                    field=code,
                    inferredType=inferred,
                    notes=notes,
                ))
        return analysis

    except Exception as e:
        logger.warning("Structured extraction failed for %s: %s", stem, e)
        text = getattr(final_message, "content", "") or ""
        return FormAnalysis(businessPurpose=text[:400])


async def _cross_form_analysis(
    forms: list[CsdForm],
    ctx: AgentContext,
    llm: ChatOpenAI,
    gateway_url: str,
    gateway_key: str,
    model: str,
    tracker,
) -> dict:
    """Compute a CrossFormReport mostly programmatically, then ask the LLM for recommendations.

    Programmatic computation avoids hallucination for structural facts (shared fields,
    control-type differences). The LLM is only asked to synthesize readable recommendations.
    """
    # --- Classifications: group by form_type ---
    classifications: dict[str, list[str]] = {}
    for form in forms:
        ft = getattr(form.meta, "form_type", "unknown") or "unknown"
        classifications.setdefault(ft, []).append(form.meta.file_name)

    # --- Inconsistencies: same field code, different control types across forms ---
    code_to_types: dict[str, dict[str, str]] = {}  # code → {file_name: ctrl_type}
    for form in forms:
        for f in form.fields:
            code_to_types.setdefault(f.code, {})[form.meta.file_name] = f.control_type

    inconsistencies: list[dict] = []
    for code, usages in code_to_types.items():
        if len(usages) < 2:
            continue
        unique_types = set(usages.values())
        if len(unique_types) > 1:
            detail = ", ".join(f"{fn}={ct}" for fn, ct in sorted(usages.items()))
            inconsistencies.append({"field": code, "issue": f"Different control types: {detail}"})

    # --- Duplicates: forms sharing a large fraction of field codes ---
    form_names = [f.meta.file_name for f in forms]
    form_field_sets = {f.meta.file_name: {fld.code for fld in f.fields} for f in forms}
    duplicates: list[dict] = []
    seen_pairs: set[frozenset] = set()
    for i, na in enumerate(form_names):
        for nb in form_names[i + 1 :]:
            pair = frozenset({na, nb})
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            sa, sb = form_field_sets[na], form_field_sets[nb]
            if not sa or not sb:
                continue
            overlap = len(sa & sb) / min(len(sa), len(sb))
            if overlap >= 0.5:
                shared = sorted(sa & sb)
                duplicates.append({
                    "forms": [na, nb],
                    "similarity": f"{overlap:.0%} field overlap ({len(sa & sb)} shared codes)",
                    "reason": f"Shared fields: {', '.join(shared[:8])}" + (" ..." if len(shared) > 8 else ""),
                })

    # --- Ask LLM for recommendations (single small call, no tool loop) ---
    summary_lines = [
        f"Forms analyzed: {', '.join(form_names)}",
        f"Form types: {dict(classifications)}",
        f"Cross-form field inconsistencies ({len(inconsistencies)}): "
        + ("; ".join(f"{i['field']}: {i['issue']}" for i in inconsistencies[:5]) or "none"),
        f"Similar form pairs ({len(duplicates)}): "
        + ("; ".join(f"{d['forms'][0]} vs {d['forms'][1]} ({d['similarity']})" for d in duplicates) or "none"),
    ]
    summary_text = "\n".join(summary_lines)

    # --- Cross-form ReAct agent for recommendations ---
    # Uses a subset of tools to investigate duplicates and inconsistencies
    # before producing recommendations (replaces single-shot LLM call).
    recommendations: list[str] = []
    try:
        all_tools = create_tools(ctx)
        cross_tool_names = {"compare_forms", "search_cross_form", "get_form_summary", "list_form_fields"}
        cross_tools = [t for t in all_tools if t.name in cross_tool_names]

        cross_prompt = (
            "You are a forms migration analyst. You have tools to investigate "
            "cross-form patterns: compare_forms, search_cross_form, get_form_summary, "
            "list_form_fields. Examine the data below, use tools to investigate the "
            "most significant inconsistencies and duplicate candidates, then produce "
            "3-7 specific, actionable recommendations. Output your final recommendations "
            "as a numbered list."
        )
        cross_agent = create_react_agent(model=llm, tools=cross_tools, prompt=cross_prompt)

        user_msg = (
            f"Cross-form analysis summary:\n{summary_text}\n\n"
            "Investigate the most significant patterns using your tools, "
            "then provide specific recommendations for the migration."
        )

        final_state: dict = {}
        all_messages: list = []
        async for chunk in cross_agent.astream(
            {"messages": [HumanMessage(content=user_msg)]},
            config={"recursion_limit": 12},
        ):
            final_state = chunk
            for node_output in chunk.values():
                if isinstance(node_output, dict):
                    for msg in node_output.get("messages", []):
                        all_messages.append(msg)

        # Track tokens
        if tracker is not None:
            react_in = react_out = 0
            for msg in all_messages:
                um = getattr(msg, "usage_metadata", None)
                if um:
                    react_in += um.get("input_tokens", 0)
                    react_out += um.get("output_tokens", 0)
            if react_in or react_out:
                tracker.record(source="cross_form", ref="batch",
                               model=model, input_tokens=react_in, output_tokens=react_out)

        # Extract final message recommendations
        if "agent" in final_state:
            msgs = final_state["agent"].get("messages", [])
            if msgs:
                raw = getattr(msgs[-1], "content", "") or ""
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                for ln in raw.splitlines():
                    cleaned = re.sub(r"^\d+[\.\)]\s*", "", ln).strip("-").strip()
                    if len(cleaned) > 10:
                        recommendations.append(cleaned)

    except Exception as e:
        logger.warning("Cross-form ReAct agent failed: %s", e)
        if inconsistencies:
            recommendations.append(
                f"Resolve control-type inconsistencies for {len(inconsistencies)} shared fields "
                f"(e.g., {inconsistencies[0]['field']})."
            )
        if duplicates:
            recommendations.append(
                f"Review {len(duplicates)} similar form pair(s) for consolidation opportunities."
            )

    return CrossFormReport(
        classifications=classifications,
        duplicates=[DuplicateReport(**d) for d in duplicates],
        inconsistencies=[InconsistencyReport(**i) for i in inconsistencies],
        recommendations=recommendations,
    ).model_dump()



async def analyze_forms(
    forms: list[CsdForm],
    field_cache: dict[str, dict],
    domain_cache: dict[str, list],
    gateway_url: str,
    gateway_key: str,
    model: str = DEFAULT_MODEL,
    tracker: Optional[TokenTracker] = None,
    form_name_hint: str = "batch",
    progress_path: Path | None = None,
) -> dict:
    """Analyze CSD forms using a LangGraph ReAct agent.

    Args:
        forms: Parsed (and optionally enriched) CSD forms.
        field_cache: {code: DictionaryInfo dict} from Phase 1.
        domain_cache: {code: [domain values]} from Phase 1.
        gateway_url: AI Gateway base URL.
        gateway_key: AI Gateway API key.
        model: Model ID (default: Qwen3-30B-A3B).
        tracker: Optional TokenTracker to record usage for each LLM call.
        form_name_hint: Label used in token events (e.g., "e2e_batch").
        progress_path: Optional path for checkpoint file. If provided,
            completed form analyses are saved after each form so the batch
            can resume on failure.

    Returns:
        Analysis dict or error fallback.
    """
    ctx = AgentContext(forms=forms, field_cache=field_cache, domain_cache=domain_cache)

    llm = ChatOpenAI(
        base_url=f"{gateway_url.rstrip('/')}/v1",
        api_key=gateway_key,
        model=model,
        temperature=0.1,
        max_tokens=8192,
        default_headers={"X-API-Key": gateway_key},
    )

    # Resume from checkpoint if available
    # On-disk checkpoint wraps form-results under "forms" key; unwrap here for stability.
    form_results: dict[str, dict] = load_checkpoint(progress_path).get("forms", {})
    if form_results:
        logger.info("Loaded checkpoint: %d forms already analyzed", len(form_results))

    # Filter to forms not yet completed
    pending_forms = [f for f in forms if f.meta.file_name not in form_results]
    if pending_forms:
        logger.info("Analyzing %d forms in parallel (%d already checkpointed)",
                     len(pending_forms), len(form_results))

        # Semaphore limits concurrent AI Gateway calls to avoid rate limiting
        max_concurrent = 5
        sem = asyncio.Semaphore(max_concurrent)

        async def _analyze_one(form: CsdForm) -> tuple[str, dict]:
            async with sem:
                stem = form.meta.file_name
                logger.info("Analyzing form %s ...", stem)
                try:
                    analysis = await _analyze_single_form(
                        form=form,
                        ctx=ctx,
                        llm=llm,
                        gateway_url=gateway_url,
                        gateway_key=gateway_key,
                        model=model,
                        tracker=tracker,
                    )
                    return stem, analysis.model_dump()
                except Exception as e:
                    logger.error("Analysis failed for %s: %s", stem, e)
                    return stem, FormAnalysis(businessPurpose=f"Error: {e}").model_dump()

        results = await asyncio.gather(*[_analyze_one(f) for f in pending_forms])
        for stem, result_dict in results:
            form_results[stem] = result_dict

        # Checkpoint after parallel batch completes
        save_checkpoint(progress_path, {"forms": form_results})

    cross = CrossFormReport().model_dump()
    if len(forms) > 1:
        cross = await _cross_form_analysis(forms, ctx, llm, gateway_url, gateway_key, model, tracker)

    return {
        "forms": form_results,
        "crossFormReport": cross,
    }
