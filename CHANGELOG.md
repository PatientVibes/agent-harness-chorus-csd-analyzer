# Changelog

## 0.2.0 — 2026-05-13

### Changed
- **Toolbox adoption.** Inline infrastructure helpers (`_sanitize_field_text`, `_retry_async`, `_is_transient`, `_load_checkpoint`, `_save_checkpoint`) replaced with imports from `agent-tool-llm-utils`. Inline `chorus_forms.csd.token_tracker.TokenTracker` swapped for `agent-tool-token-tracker`. No behavior change; on-disk checkpoint format preserved (in-flight v0.1.0 checkpoints keep resuming).
- The 4 `tracker.record(...)` call sites updated for the new tool's kwarg name (`form_name=` → `ref=`).

### Fixed
- `AI_GATEWAY_MODEL` env var was documented in the README but never read; now wired through `app.py` to `analyze_forms(model=...)` and surfaced in `GET /status` as `ai_model`.
- `tests/test_ai_client.py` over-broad file-level `importorskip("chorus_forms", ...)` was suppressing `TestExtractJson` (which has no chorus_forms dependency). Narrowed to per-method `importorskip` so `TestExtractJson` (5 tests) and `TestAIGatewayClient.test_init_*` (2 tests) now run in public CI. The new `TestAIGatewayModelEnvVar` (G6a) still skips in public CI because `app.py` imports `chorus_forms` at module scope; it runs locally where chorus_forms is installed.

### Dependencies
- New: `agent-tool-llm-utils` (pinned to commit `cfdf9aba6aa0ccc9c37860a9bef53853e4504237`)
- New: `agent-tool-token-tracker` (pinned to commit `d16943fdb0516f386ac798e1527526775ef52af1`)

## 0.1.0 — 2026-05-12

Initial release. Migrated from `d:/ai-agents/chorus-agent/web/` as part of the chorus-agent split (see `D:/ai-agents/docs/superpowers/specs/2026-05-12-reference-agents-migration-design.md`).

- FastAPI app + LangGraph ReAct agent for AWD CSD form analysis.
- 12-component agent harness implementation.
- Consumes `agent-tool-chorus-v1-client` (PatientVibes) via `[tool.uv.sources]` git URL.
- Vendors `csd-form-analysis` skill's system prompt + AWD reference knowledge at package-local paths.

**Known limitation:** depends on the upstream private `chorus_forms` package (CSD parser, models, XML/UXB builders, preview renderer) which is not declared in `pyproject.toml`. The harness will not run end-to-end without it. This snapshot is study-grade reference material; runnable use requires access to the upstream package.
