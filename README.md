# agent-harness-chorus-csd-analyzer

FastAPI web app + LangGraph ReAct agent for SS&C AWD CSD binary form analysis and conversion to Chorus Classic XML. 12-component agent harness.

## Important: upstream dependency

This harness depends on the private `chorus_forms` package (CSD parser, models, XML/UXB builders, preview renderer) which is **not declared in `pyproject.toml` and is not available on PyPI or any public registry**. Public consumers can read the code but cannot run the harness end-to-end. This is unchanged from the predecessor `d:/ai-agents/chorus-agent/` snapshot.

## What it does

1. Upload CSD/LKP binary files
2. Parse and enrich fields (from v2-api or AWD v1 field registry via `chorus_v1_client`)
3. Run AI agent analysis — identifies field types, type promotions, risks, DLL hooks
4. Convert to Chorus Classic XML
5. (Optional) Import directly to a live Chorus server

## Quick start

```bash
uv pip install -e ".[dev]"
# Requires chorus_forms package available in the environment.
uvicorn chorus_csd_analyzer.app:app --reload --port 8000
```

Open `http://localhost:8000`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `AI_GATEWAY_URL` | No | SS&C AI Gateway base URL |
| `AI_GATEWAY_KEY` | No | AI Gateway API key |
| `AI_GATEWAY_MODEL` | No | Model ID (default: `Qwen/Qwen3-30B-A3B`) |
| `CHORUS_URL` | No | AWD server base URL for field registry lookup |
| `CHORUS_USER` | No | AWD API username |
| `CHORUS_PASS` | No | AWD API password |
| `CHORUS_CONTEXT` | No | AWD context path (default: `awdServer`) |

## 12-component harness implementation

| Component | Implementation |
|---|---|
| Orchestration | LangGraph `create_react_agent` per form + cross-form ReAct agent |
| Tools | `list_form_fields`, `get_field_detail`, `get_domain_values`, `get_form_summary`, `search_cross_form`, `compare_forms`, `suggest_field_type_promotion` |
| Memory | In-process `AgentContext` (forms, field_cache, domain_cache); checkpoint JSON for resume |
| Context mgmt | Per-form ReAct loop scoped to one form; cross-form agent gets aggregated summaries only |
| Prompt construction | System prompt vendored from `csd-form-analysis` skill, `{knowledge}` filled from `awd_reference.md` |
| Output parsing | `with_structured_output(FormAnalysis)` Pydantic models |
| State | Checkpoint JSON (parallel form analysis), AgentContext caches |
| Error handling | Transient HTTP retry with backoff; structured-extraction retry on verification failure |
| Guardrails | Input sanitization (`_sanitize_field_text`), prompt-injection filtering, output truncation |
| Verification | `_verify_analysis` checks coverage + non-existent codes + DLL-hook hallucination; one retry |
| Subagent orchestration | Per-form parallel ReAct (semaphore-bounded); cross-form ReAct subagent |
| Token tracking | `TokenTracker` records every LLM call source / form / model / tokens / latency |

## Skill

The analysis system prompt lives at [`PatientVibes/agent-skills/plugins/csd-form-analysis/`](https://github.com/PatientVibes/agent-skills). This repo vendors a copy at `src/chorus_csd_analyzer/prompts/system_prompt.md` for standalone operation.

## License

MIT. See `LICENSE`.

## Provenance

Migrated 2026-05-12 from `d:/ai-agents/chorus-agent/web/`. See `D:/ai-agents/docs/superpowers/specs/2026-05-12-reference-agents-migration-design.md`.
