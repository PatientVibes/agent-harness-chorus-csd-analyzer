# Changelog

## 0.1.0 — 2026-05-12

Initial release. Migrated from `d:/ai-agents/chorus-agent/web/` as part of the chorus-agent split (see `D:/ai-agents/docs/superpowers/specs/2026-05-12-reference-agents-migration-design.md`).

- FastAPI app + LangGraph ReAct agent for AWD CSD form analysis.
- 12-component agent harness implementation.
- Consumes `agent-tool-chorus-v1-client` (PatientVibes) via `[tool.uv.sources]` git URL.
- Vendors `csd-form-analysis` skill's system prompt + AWD reference knowledge at package-local paths.

**Known limitation:** depends on the upstream private `chorus_forms` package (CSD parser, models, XML/UXB builders, preview renderer) which is not declared in `pyproject.toml`. The harness will not run end-to-end without it. This snapshot is study-grade reference material; runnable use requires access to the upstream package.
