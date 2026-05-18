# `agent-app-chorus-csd-analyzer` — Design

**Date:** 2026-05-18
**Status:** v3 — co-plan reviewed, Plan 1+2 shipped, Plan 4 client decision locked
**Author:** Chris Moore (with Claude)

## Revision history

- **v1 (2026-05-18)**: Initial outline.
- **v3 (2026-05-18)**: Plan 4 client decision after deep cail + chorus-mcp-server review.
  - **Chorus client = `chorus-mcp-server` as a library import** (NOT a cail line-by-line port, NOT an in-app port). User picked this over the hybrid recommendation, accepting the heavier transitive dep tree in exchange for inherited operations, async/httpx/tenacity/JWT done, typed pydantic models. The MCP server itself is NOT started — we import the client class directly.
  - **chorus-mcp-server is being redone in parallel** — the exact API surface (`ChorusClient`'s method names, signatures, model shapes) is **not locked** by this spec. Plan 4 will pin against whatever the refactored package exposes; the contract this app needs is captured in §"Live Chorus import" as a method-level wish list rather than a code dependency.
  - **PyInstaller implication**: bundle list in §"Packaging and distribution" includes `chorus_mcp_server` and its native-lib transitive (`cryptography`) — exact `collect_all` list re-verified in Plan 5 against whatever the refactored package ships.
  - **Environment login UX**: per-session form (base_url + user + pass). Persist recently-used `base_url`s to a new `chorus_environments` SQLite table (no credential columns ever); on next launch, the ChorusImport pane shows a dropdown of recent URLs.
  - **What I learned in cail that's still relevant**: API quirks (`pages` not `page` for businessareas; transaction+case double-fetch for work types; `{"$": "X", "@href": "Y"}` response shape) — these are inputs to the chorus-mcp-server redo, not things this app should re-implement.
- **v2 (2026-05-18)**: Co-plan review with Gemini 2.5 Pro applied. Changes:
  - Harness dep: floor pin → editable path source (harness is private, not on PyPI).
  - Repo shape: added `app/config.py`, `app/db.py`, `app/launcher.py` (missing from v1; required by the card-extractor precedent).
  - State directory: explicit frozen-aware resolution via `platformdirs` (the v1 `app/state/` path would be read-only inside the bundled binary).
  - PyInstaller `collect_all` list: enumerated all five top-level package names — `chorus_csd_analyzer`, `chorus_forms`, `chorus_v1_client`, `llm_utils`, `token_tracker` (v1 missed the three `agent-tool-*` siblings).
  - Added §"Important: upstream dependency" caveat for public consumers.
  - Added §"Risks" section enumerating five concrete failure modes.
  - Added packaging smoke test (`RUN_PACKAGING_SMOKE=1`) to verification.
  - cail port: explicitly named the constants and behaviours to preserve (pagination, retries, timeout).
  - Chat tools: kept 5-tool surface for v1 but documented the 3-tool consolidation as a Plan 3 implementation-time decision.

## Important: upstream dependency

This app depends on the harness, which depends on the private `chorus_forms` package — not declared in `pyproject.toml` and not on any public registry. Public consumers can read the code but cannot run `uv sync` end-to-end without `chorus_forms` available in the environment. This constraint is inherited transitively from `agent-harness-chorus-csd-analyzer` (see that repo's README §"Important: upstream dependency"). The app's README must surface the same warning.

## Goal

Create a new sibling repo, `agent-app-chorus-csd-analyzer`, that packages an installable single-machine app around the existing `agent-harness-chorus-csd-analyzer` library. The app provides a file-drop intake for CSD/LKP binary files, a tabbed previewer (JSON / Chorus Classic XML / per-form summary) for analysis results, an interactive chat session in which the agent can propose overrides to its own analysis, and a guided "import to live Chorus" flow built on the `cail` REST patterns.

The split is deliberate: the **app stays static** (UI, packaging, session glue, live-import flow) while the **agent harness evolves** independently as a versioned dependency. This mirrors the `agent-app-card-extractor` / `agent-harness-card-extractor` split already shipped.

## Non-goals

- Multi-user, multi-tenant, or hosted operation. Single user, single machine, offline-capable except for the optional live-Chorus import step.
- Authentication, role separation, audit trails beyond local SQLite history.
- A new analysis agent, new prompts, or new field-type heuristics — those changes belong in the harness repo.
- Replacing the harness's existing HTMX/Jinja2 UI in this iteration. The new app **shadows** the old UI; the old one keeps working until separately retired.
- Replacing `cail` for batch CSV-to-Chorus uploads. The new app borrows `cail`'s `ChorusAPI` client patterns for the import step, not its full CSV-wizard surface.

## Repo shape

```
agent-app-chorus-csd-analyzer/
  app/                              # FastAPI backend
    __init__.py
    main.py                         # ASGI entry, lifespan owns durable state
    config.py                       # AppConfig — frozen-aware state_dir + web_dist resolution
    db.py                           # SQLite schema + init_db (only module that touches the DB)
    launcher.py                     # PyInstaller entry: picks port, starts uvicorn, opens browser
    routes/
      uploads.py                    # POST /uploads — CSD/LKP binary, returns session_id
      analyses.py                   # GET /analyses/:id, GET /analyses/:id/xml, GET /analyses/:id/summary
      chat.py                       # WS /chat/{analysis_id} — interactive session
      chorus_import.py              # POST /chorus/login, POST /chorus/import — live import (Plan 4)
      health.py                     # GET /health — readiness probe (used by smoke test)
    services/
      pipeline.py                   # async wrapper around harness analyze_forms + convert
      serializers.py                # FormAnalysis ↔ XML ↔ per-form summary
      chat_agent.py                 # LangChain loop with override-tool surface
      chorus_client.py              # cail-derived REST client (auth + CSRF + pagination + retries)
  web/                              # React + Vite frontend
    src/
      App.tsx
      panes/FileDrop.tsx            # drag-drop binary upload
      panes/Previewer.tsx           # tabs: JSON | XML | per-form summary
      panes/Chat.tsx                # streaming messages + override-proposal cards
      panes/ChorusImport.tsx        # 4-step wizard mirroring cail (login → confirm → execute → results)
    # built output goes to web/dist/, bundled into web_dist/ inside the PyInstaller artifact
  packaging/
    pyinstaller.spec                # single-file installer build
    build.py                        # convenience wrapper (mirrors card-extractor)
  tests/
    test_packaging_smoke.py         # RUN_PACKAGING_SMOKE=1 integration test
  pyproject.toml                    # depends on harness via [tool.uv.sources] editable path
  README.md
```

**State directory is NOT inside the repo at runtime.** In dev (source checkout) it defaults to `./app/state/`; when frozen (`sys.frozen` set) it resolves to `platformdirs.user_data_dir("agent-app-chorus-csd-analyzer", "chorus-csd-analyzer")`. Override via `APP_STATE_DIR`. This matches the card-extractor pattern in `agent-app-card-extractor/app/config.py`.

## Architecture

Four logical layers, each independently testable:

1. **Pipeline service** — async wrapper around the harness's `enrich_forms` + `analyze_forms` + `convert_files`. Owns session lifecycle (`pending` → `parsing` → `enriching` → `analyzing` → `converting` → `complete` | `failed`) and persists the canonical `FormAnalysis` collection to `state/outputs/<session_id>.json` plus the rendered XML to `state/outputs/<session_id>.xml`.
2. **Presentation layer** — read-only views over the canonical analysis JSON. Three tabs in the previewer: **JSON** (raw `FormAnalysis` collection), **XML** (Chorus Classic, server-side rendered from the harness's converter), **per-form summary** (field counts, type promotions, risks, DLL hooks, cross-form references — derived from the same on-disk JSON).
3. **Chat session** — stateful WebSocket-backed agent loop scoped to a single analysis. The chat agent gets read access to the current analysis and a tool set for proposing overrides. User accepts or rejects each proposed override; accepted overrides update the canonical JSON and the rendered XML.
4. **Chorus import** — optional terminal step. After a user accepts the analysis, they can authenticate to a live Chorus AWD server and push the converted forms/fields via the REST endpoints already established by `cail`. State of the import (per-form success/failure, retry attempts) is recorded in SQLite for audit.

## Data flow

1. **Upload** — user drops a `.csd`/`.lkp` binary into the file-drop pane. Frontend `POST`s to `/uploads`; backend stores the file in `state/uploads/`, creates a session row in SQLite with `status=pending`, returns `session_id`.
2. **Pipeline** — background task picks up the pending session, invokes the harness (`parse_csd_file` → `enrich_forms` → `analyze_forms` → `convert_files`), persists the `FormAnalysis` collection JSON and rendered Chorus Classic XML to `state/outputs/<session_id>.{json,xml}`. Status moves to `complete` (or `failed` with a captured error).
3. **Preview** — frontend polls `/analyses/:id` until `complete`, then fetches whichever tab is active: `/analyses/:id` (JSON), `/analyses/:id/xml` (XML), `/analyses/:id/summary` (derived per-form roll-up). All three views derive from the same canonical JSON on disk.
4. **Chat** — user opens the chat pane on a completed analysis. Frontend connects to `WS /chat/:analysis_id`. The backend instantiates a chat agent seeded with the current `FormAnalysis` collection, the original CSD file metadata, and the override tool surface. User and agent exchange messages; tool calls and override proposals stream to the UI.
5. **Override application** — when the agent proposes an override (e.g., `override_type_promotion`), the proposal arrives at the UI as a diff card. User accepts → backend mutates the canonical JSON, re-renders the XML, appends an entry to the override log, and the previewer refreshes. Rejected overrides are recorded but not applied.
6. **Live import (optional)** — after the user is satisfied with the analysis, they open the Chorus Import pane: log in (Basic auth + CSRF), select a business area and work type, confirm the field mapping the agent produced, then execute the import. Per-form/per-instance results stream back; failures are exported as a downloadable error report.

## Chat agent tools

The chat agent uses the same LLM family as the harness's analyzer (configurable via env). Five tools — read-only grounding plus four override proposals. None of these mutate state directly; only the user's accept action does.

- **`read_field(form_name: str, field_path: str) -> value`** — read a value from the current analysis (e.g., `forms[0].fields[3].suggested_type`). Used for grounding before proposing an override.
- **`override_type_promotion(form_name: str, field_path: str, new_type: str, reason: str) -> proposal_id`** — propose a different field-type promotion than what the agent's first pass produced. Surfaces as a diff card.
- **`mark_field_ignore(form_name: str, field_path: str, reason: str) -> proposal_id`** — propose excluding a field from the XML output. Common need for legacy noise fields the agent kept.
- **`re_examine_form(form_name: str, focus: str) -> observation`** — re-run a focused single-form analysis with a user-supplied directive (e.g., "this form is a calculation table, not a data form"). Returns the new observation; the agent decides whether to follow up with an override proposal.
- **`update_dll_hook(form_name: str, field_path: str, hook_name: str, reason: str) -> proposal_id`** — propose associating a DLL hook with a field, or removing one.

The agent does not have a "commit" tool — only the user's accept action mutates state.

**Design alternative (revisit during Plan 3 implementation):** card-extractor's chat ships with only two tools — `read_field` and `set_field` — collapsing all CRUD-on-FormAnalysis into a single generic mutator. A leaner alternative for this app is `read_field`, `re_examine_form`, and a single `override_field(form_name, field_path, property, new_value, reason)` that subsumes type-promotion, ignore-flag, and DLL-hook overrides. The trade-off is more constrained tool semantics (the LLM picks correct property names) versus a smaller agent decision surface. Decide at Plan 3 kickoff once the per-property override schemas are sketched.

## Live Chorus import (Plan 4)

The Chorus REST client is consumed from the `chorus-mcp-server` sibling repo as a library import. We import the client class directly and use it as a Python library; **we do not run an MCP server** alongside the app. This decision was locked in v3 after evaluating three alternatives (port cail line-by-line, hybrid port that copies modern patterns, library import).

**chorus-mcp-server is being redone in parallel.** The exact class names, method signatures, and model shapes below are a **wish list** describing what this app needs from the refactored package, NOT a contract against the current code. Plan 4 implementation begins after the refactored client surface stabilises; the app's `app/services/chorus_client.py` is a thin adapter that translates wish-list calls into whatever the refactored package exposes.

**Why this client, not a fresh port:**

- Async + `httpx` + `tenacity`-based retry already implemented.
- JWT + HTTP Basic dual auth with auto-refresh on 401 (cail only does Basic).
- Pydantic typed request/response models (`CreateInstanceRequest`, `ChorusInstance`, …) — no raw-dict parsing in our code.
- Cail's API quirks (`pages` not `page` for businessareas, transaction+case double-fetch for work types, `{"$": "X", "@href": "Y"}` response shape) are already handled inside `ChorusClient`.
- Future Chorus operations get added in the upstream sibling repo and we inherit them automatically.

**Dependencies the import adds** (visible to PyInstaller in Plan 5):

```
chorus-mcp-server -> mcp, sse-starlette, websockets, prometheus-client,
                     structlog, marshmallow, aiosqlite, PyJWT, cryptography,
                     lxml (already a harness dep)
```

The MCP SDK, sse-starlette, and websockets are imported transitively but **never executed at runtime** — `app/services/chorus_client.py` only touches `chorus_mcp_server.client` + `.models`. We accept the bundle size in exchange for not maintaining a parallel REST client.

**Operations this app needs** (wish list — final method names depend on the chorus-mcp-server redo):

- **Authenticate** (Basic and/or JWT) given `base_url + user + pass`, returning an opaque session/client handle. Must capture CSRF on the same call if the underlying API requires it.
- **List business areas** (LOBs) — for the wizard's "business area" dropdown.
- **List work types for a business area** — for the wizard's "work type" dropdown. Must cover both transaction and case work types (cail's double-fetch quirk should be hidden inside the client, not bubbled up to us).
- **Create instances** — accept a list of typed instance-creation requests, return per-instance success/failure with enough detail to drive a CSV failure report.
- **Optional**: list recent instances / search — for verification after import (not strictly needed for Plan 4 MVP but nice-to-have for the wizard's "results" step).

The app's `app/services/chorus_client.py` will be a thin adapter that exposes exactly these five operations to the rest of the app and translates them into whatever the refactored chorus-mcp-server package provides. If the refactor changes API shapes later, only the adapter needs to follow.

**Wizard flow** (`ChorusImport.tsx`):

1. **Connect**: pick from recent `base_url`s (or type a new one) → enter user + pass → `authenticate_*`.
2. **Configure**: pick business area + work type from dropdowns populated by `get_business_areas` / `get_work_types`.
3. **Confirm**: preview the field mapping the agent produced.
4. **Execute**: stream per-instance results; failed instances collected for download as CSV; success/fail counters persist to the `chorus_imports` SQLite table.

**Credential handling** (unchanged from v2 risk-table):

- Password held in the in-memory `ChorusClient` instance only; never persisted.
- `chorus_imports` SQLite table stores `base_url`, `business_area`, `work_type`, counts, timestamps — **no password / auth_token / CSRF columns**.
- A new `chorus_environments` SQLite table stores `base_url` + `last_used_at` + `display_name` (optional) only — no auth columns ever. This powers the "recent servers" dropdown without persisting credentials. Users can delete entries from the UI.

## Boundary: harness vs. app

| Lives in `agent-harness-chorus-csd-analyzer` (evolving) | Lives in `agent-app-chorus-csd-analyzer` (static) |
|---|---|
| CSD parse / enrich / analyze / convert pipeline | File-drop HTTP endpoint, session queue, SQLite state |
| LangGraph ReAct agent + per-form / cross-form prompts | JSON ↔ XML ↔ summary serializers (presentation concern) |
| `chorus_v1_client` field-registry integration (read) | Chorus REST client for instance creation (write — live import) |
| `chorus_forms` private package dependency | Chat agent loop + override-proposal tools |
| Prompt templates + `awd_reference.md` knowledge | UI for diffing, accepting, and rejecting overrides |
| Existing HTMX/Jinja2 web UI (kept until separately retired) | PyInstaller packaging / installer |
| `run_batch` CLI (none currently — but parity slot if added) | Per-session chat history and override audit log |

The app depends on the harness via `[tool.uv.sources]` with an editable path source:

```toml
[tool.uv.sources]
agent-harness-chorus-csd-analyzer = { path = "../agent-harness-chorus-csd-analyzer", editable = true }
```

This matches the established `agent-app-card-extractor` / `agent-harness-card-extractor` pattern (see `D:/agent-app-card-extractor/pyproject.toml`). A floor-pin against PyPI is **not** an option because the harness is not published — it depends transitively on the private `chorus_forms` package and lives only as a GitHub repo. Harness "upgrades" are a git pull in the sibling directory; there is no version-bump churn in this repo's `pyproject.toml`.

## Local state

Resolved via `app/config.py::AppConfig.from_env()` — `state_dir` is `APP_STATE_DIR` env override, else `platformdirs.user_data_dir(...)` when `sys.frozen`, else `./app/state/` in source-checkout dev. Frozen-mode paths land at:

- Windows: `%LOCALAPPDATA%\chorus-csd-analyzer\agent-app-chorus-csd-analyzer\`
- macOS:   `~/Library/Application Support/agent-app-chorus-csd-analyzer/`
- Linux:   `~/.local/share/agent-app-chorus-csd-analyzer/`

Contents:

- **SQLite (`<state_dir>/analyses.db`)** —
  - `sessions` table: id, status, created_at, source_filename, output_path, error.
  - `chat_messages` table: session_id, role, content, tool_calls, ts.
  - `override_log` table: session_id, form_name, field_path, kind (`type_promotion` | `ignore` | `dll_hook`), old_value, new_value, reason, accepted, ts.
  - `chorus_imports` table: session_id, base_url, business_area, work_type, started_at, completed_at, success_count, fail_count. **No password or auth_token columns** — credentials live only on the in-memory `ChorusClient` for the import's duration.
  - `chorus_environments` table (Plan 4): base_url (PK), display_name (nullable), last_used_at. Powers the "recent servers" dropdown in the ChorusImport pane. **No credential columns ever** — users delete entries from the UI; deleting an entry only forgets the URL, never invalidates a live session.
- **Filesystem** — `<state_dir>/uploads/` for raw binaries, `<state_dir>/outputs/` for canonical JSON + rendered XML. Both keyed by `session_id`.

No daemon, no external services (until the user explicitly triggers a live Chorus import).

## Packaging and distribution

Same shape as card-extractor: a single PyInstaller spec produces a one-file executable. The Vite frontend builds to `web/dist/` and is bundled at `<bundle>/web_dist/`; PyInstaller bundles the FastAPI app, the harness package, the private `chorus_forms` package, all sibling `agent-tool-*` packages, the static assets, and a Python runtime. Launching the binary picks a free localhost port, starts uvicorn, opens the default browser.

**Critical: enumerate every top-level package name `collect_all` must walk.** Card-extractor's `packaging/pyinstaller.spec` (lines 21-29) explicitly lists six packages because `collect_all` on the umbrella alone causes `ModuleNotFoundError: No module named 'llm_utils'` at first launch. Mirror that pattern. For this app the list is:

```python
harness_packages = [
    "chorus_csd_analyzer",   # the harness itself
    "chorus_forms",          # private upstream — CSD parser, models, XML/UXB builders
    "chorus_v1_client",      # agent-tool-chorus-v1-client (REST client)
    "llm_utils",             # agent-tool-llm-utils (retry, sanitize, extract_json, checkpoint)
    "token_tracker",         # agent-tool-token-tracker (LangChain usage capture)
    "chorus_mcp_server",     # Plan 4 live-import client (we import .client.ChorusClient only)
]
```

`chorus_mcp_server` adds `cryptography` (native-libs — PyInstaller has hooks-contrib coverage but verify in Plan 5), `PyJWT`, `mcp`, `sse-starlette`, `websockets`, `prometheus-client`, `structlog`, `marshmallow`, `aiosqlite`. These are bundled in full even though most are never executed (we never start the MCP server). If bundle size becomes a problem in Plan 5, the mitigation is to vendor `chorus_mcp_server/client.py` + `auth.py` + `models.py` into our repo and drop the package dependency.

`uvicorn` hidden imports (websockets + lifespan loaders) are added to `hiddenimports` exactly as in card-extractor's spec. The launcher is `app/launcher.py`, mirroring card-extractor.

## XML serialization

The XML form is produced by the harness's existing `convert_files` (Chorus Classic format). The app renders it as-is and the previewer's XML tab displays it pretty-printed. The app does not re-implement XML generation — it consumes what the harness emits and re-renders only when an accepted override changes the underlying `FormAnalysis`.

## Testing strategy

- **Pipeline service** — integration test against a fixture CSD binary, asserting the canonical JSON and XML outputs exist and match the harness's direct output.
- **Serializers** — unit tests for the per-form summary derivation.
- **Chat tools** — unit tests for each tool against a fixed analysis, with the agent loop stubbed.
- **Override application** — test that accepted overrides mutate the on-disk JSON, re-render XML, and append to the override log; rejected overrides append-only.
- **Chorus import** — `chorus_client.py` tests use a mocked `httpx` transport. Integration test against a real Chorus server is gated by env and skipped in CI.
- **Packaging smoke (CRITICAL)** — `tests/test_packaging_smoke.py` launches the built binary, waits for the `"Selected port N"` sentinel emitted by `app/launcher.py` only after `wait_for_server` confirms uvicorn is listening, then hits `/health` and posts a fixture CSD to `/uploads` and polls `/analyses/:id` until `complete`. This is the only check that catches missing `collect_all` packages, broken `platformdirs` paths, or a wrongly-bundled `chorus_forms`. Gated by `RUN_PACKAGING_SMOKE=1` env var; marked `pytest.mark.integration`; skipped in default CI. Same shape as `D:/agent-app-card-extractor/tests/test_packaging_smoke.py`.

The harness's existing tests are not duplicated.

## Plan order

1. **Plan 1 — Backend foundation**: FastAPI scaffolding (`app/main.py` with `lifespan`), `app/config.py` with **frozen-aware** `state_dir`/`web_dist` resolution (do this in Plan 1, not Plan 5 — the design must be packaging-ready from the start), `app/db.py` + schema, `/uploads`, `/analyses/:id` (JSON/XML/summary), `/health`. Pipeline service wraps the harness.
2. **Plan 2 — Frontend**: Vite + React + TS scaffold, FileDrop pane, Previewer with three tabs. Vite dev proxy forwards `/uploads`, `/analyses`, `/chorus`, `/health` to the backend on `:8000`.
3. **Plan 3 (a + b) — Chat**: WS `/chat/{analysis_id}`, override-log table, `ChatAgentService` with the chat-tool surface (5 vs 3 — see §"Chat agent tools" alternative); React chat pane with override-proposal cards.
4. **Plan 4 — Live Chorus import**: `chorus_client.py` (cail-derived, with pagination + retry constants preserved), `/chorus/*` routes, `ChorusImport.tsx` wizard pane.
5. **Plan 5 — Installer**: `packaging/pyinstaller.spec` (all 5 `collect_all` packages enumerated), `app/launcher.py`, `packaging/build.py` convenience wrapper, packaging smoke test.

## Risks

Explicit failure modes to design against — most are unavoidable but each needs a documented mitigation.

| Risk | Mitigation |
|---|---|
| **Agent proposes an override that produces invalid Chorus Classic XML** (e.g., a type promotion that the converter rejects). | After accepting an override, the pipeline re-runs `convert_files` on the mutated `FormAnalysis`. If conversion raises, the override is rolled back from `state/outputs/<session_id>.json` and the override-log row is marked `accepted=false, error=<message>`. The UI surfaces the rollback as a toast on the override card. |
| **WebSocket disconnect mid-stream** (tool call partway through, override proposal in flight). | Each chat-message and tool-call append is its own SQLite transaction. On reconnect, the frontend re-fetches `GET /chat/{analysis_id}/history` and re-renders. Pending override proposals are persisted as `decision='pending'` rows in `override_log`; on reconnect the UI shows them in the same in-flight state. |
| **Partial-success live import** (some forms POST successfully, others fail mid-batch). | Import is per-instance, never transactional across instances. Per-row success/failure persists to `chorus_imports` + a sidecar `<state_dir>/outputs/<session_id>.import-errors.csv` downloadable from the UI. User can resume by reposting only the failed-row subset; idempotency keyed on the row's natural key plus a retry counter. |
| **Credential leakage via SQLite**. | `chorus_imports` table stores only `base_url`, `business_area`, `work_type`, `started_at`, `success_count`, `fail_count`. No password / auth_token / CSRF columns. Credentials live exclusively on the in-memory `ChorusClient` instance held by the import-session task; cleared on import completion or session end. |
| **Port collision** with another process (typically the harness's own dev FastAPI on `:8000`). | `app/launcher.py` uses `find_free_port()` (OS-assigned port via `bind(('127.0.0.1', 0))`) — no hardcoded port at runtime. The `"Selected port N"` sentinel prints after `wait_for_server` confirms uvicorn is listening, so the smoke test never races. |
| **Public consumer cannot `uv sync`** because `chorus_forms` isn't on a registry. | App README opens with the same "Important: upstream dependency" callout as the harness README. `pyproject.toml` does not pretend `chorus_forms` is installable from PyPI. |

## Open questions resolved during outline review

- **Boundary**: Option A — new sibling app shadows the existing UI in the harness. Harness UI keeps working; new app is additive.
- **Chat tools**: 5 tools as listed (`read_field`, `override_type_promotion`, `mark_field_ignore`, `re_examine_form`, `update_dll_hook`).
- **Live Chorus import**: in-scope for v1, as Plan 4. Build on the `cail` `ChorusAPI` patterns; port from `requests` to `httpx`.
- **Frontend tabs**: JSON / XML / per-form summary.
- **Harness dep pin**: floor pin (`>=0.2.0`). Rationale: matches card-extractor's pattern; lets fixes flow through automatically without forcing exact-version churn; the harness is a sibling we control, so any breakage from a floor-pin is fast to detect.

## Out of scope / explicit deferrals

- Hosted / multi-user mode.
- Authentication (beyond passing through Chorus credentials for live import).
- Mobile UI.
- Real-time collaborative editing of a single analysis.
- A separate REST API for third-party clients (the HTTP surface is an implementation detail of the installed app).
- Auto-update mechanism for the installer.
- Retiring the harness's existing HTMX/Jinja2 UI — separate cleanup once the new app is proven.
- Full CSV-to-Chorus wizard (`cail`'s primary use case) — `cail` continues to serve that workflow.
