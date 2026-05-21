# Plan 4 Rebuild — Form Import via `import_user_screen` — Design

**Date:** 2026-05-20
**Status:** Design (pre-implementation)
**App repo:** [`D:/agent-app-chorus-csd-analyzer`](../../../../agent-app-chorus-csd-analyzer/)
**Supersedes (partially):** the Plan 4 sections of [`2026-05-18-agent-app-chorus-csd-analyzer-design.md`](2026-05-18-agent-app-chorus-csd-analyzer-design.md). The umbrella spec's other plans (1, 2, 3, 3b, 3c, 5) are unaffected.
**Author:** Chris Moore (with Claude)

## Background

The shipped Plan 4 (PRs #1 and #2 in the app repo) implements a "Live Chorus import" wizard built around `ChorusClient.create_instances(business_area, work_type, field_values)`. The 2026-05-20 manual UI walkthrough surfaced that **this is the wrong operation**: forms are global, standalone schema objects in Chorus — they don't belong to a Business Area — so the wizard's BA/work-type/queue/process-name steps answer the wrong question. The correct operation is **form deployment** via the legacy AJAX portal layer (`POST /awd/portal?jobName=tcAJAXImportUserScreen`), captured in detail in [the form-import discovery memo](../../../../../Users/chris/.claude/projects/d--agent-harness-chorus-csd-analyzer/memory/reference_form_import_discovery.md).

`chorus-mcp-server` PR #62 (merged 2026-05-20) ships two new async helpers in `transport/portal.py`:
- `portal_action(config, job_name, *, fields, files) -> JobReturn` — generic `/awd/portal?jobName=*` dispatcher.
- `import_user_screen(config, *, form_name, xml) -> JobReturn` — typed shim that filters the canonical "Job request node not found!" warning fired as XSLT fallthrough.

These are **pure Python async functions** (the transport package's `__init__.py` is explicit: *"pure HTTP / SOAP clients, no MCP awareness"*). Calls are stateless and self-authenticating — each opens a fresh `httpx.AsyncClient`, runs the Basic-auth + JSESSIONID + `csrf_token` bootstrap, and submits the multipart POST. This eliminates the long-lived session lifecycle that Plan 4's current implementation maintains.

## Goal

Replace Plan 4's instance-creation wizard with a minimal two-step form-deployment flow:

1. **Connect** — collect Chorus base URL + credentials, validate via a single REST ping.
2. **Review & Import** — per-form table with editable form names, "Import N forms" action, per-row streaming results.

Decoupled from BA/work-type/queue concepts entirely. Drops ~5000 lines of wrong-abstraction code and ~91 backend unit tests + 3 soak tests + ~10 frontend tests; rebuilds as ~500 lines + ~18 backend + 9 frontend + 3 soak.

## Non-goals

- **No MCP-tool wrapper around `portal_action`.** App calls the helper as an in-process Python import (`from chorus_mcp_server.transport import import_user_screen`), matching the existing app pattern with `ChorusClient`. An MCP-tool exposure is a chorus-mcp-server concern, deferred until something needs it (likely Plan 3c's chat agent, not this rebuild).
- **No batch overwrite/idempotency UI.** Behavior on name-collision is unknown (open question, probed in soak test). If collisions overwrite silently, no UI change. If they error, a follow-up adds a "Force overwrite" affordance.
- **No persistent import-job records.** Form import is one-shot against an external system; results aren't queryable analysis artifacts. Shown inline, gone on page refresh.
- **No credential persistence beyond the React component's lifetime.** Base URL is cached in `localStorage`; username/password live in component state only. Re-typed each new session.
- **No retry-policy UX.** Failed rows show ✗ + the Chorus response; user clicks Import again to retry. Adding queue/backoff is premature.
- **No changes to Plans 1/2/3/3b/3c/5.** This rebuild is scoped to the chorus-import surface.

## Architecture

### Backend module layout (replaces 4 files with 2)

**Delete (PR 1):**
- `app/services/chorus_client.py`
- `app/services/chorus_mapping.py`
- `app/services/chorus_session.py`
- `app/services/chorus_import_runner.py`
- `app/routes/chorus_import.py`
- All DTOs referencing `business_area_name`, `work_type`, `queue_name`, `process_name`, `chorus_environments`.
- The `chorus_*` tables in SQLite (drop in `db.py` schema; no migration — local dev DB).
- The `db.py` helper functions tied to those tables: `record_environment_use`, `list_recent_environments`, `create_chorus_import`, `update_chorus_import`, `get_chorus_import` (and any imports of these in routes/services).

**Required `app/main.py` cleanup in PR 1** (verified against current file):
- Line 22 — remove `chorus_import` from the `app.routes` import.
- Lines 25–26 — delete the `ImportRunRegistry` and `ChorusSessionManager` imports.
- Lines 65–75 — delete the `chorus_sessions`/`chorus_imports`/`chorus_import_tasks` lifespan setup (the entire "Plan 4 …" / "Plan 4.5 …" blocks).
- Lines 81–82 — delete the `chorus_sessions.stop_sweep_task()` / `close_all()` teardown.
- Line 90 — delete `app.include_router(chorus_import.router)`.
- After cleanup, `make ci-local` (or the equivalent) must pass — that's the gate on PR 1.

**Add (PR 2):**

`app/services/form_import.py` (~150 lines)
```python
@dataclass(frozen=True)
class ImportCredentials:
    base_url: str        # canonical REST v1 root, e.g. https://host/devapp/awdServer/awd/services/v1
    username: str
    password: str

@dataclass(frozen=True)
class ImportResult:
    form_name: str
    ok: bool
    chorus_code: int                # JobReturn.code; 0 = success
    description: str                # JobReturn.description (verbatim from Chorus)
    warnings: list[dict[str, str]]  # JobReturn.warnings, benign noise already filtered by the helper
    raw_excerpt: str                # first 4KB of JobReturn.raw, for diagnostics on failure

async def import_form(
    creds: ImportCredentials,
    form_name: str,
    xml_bytes: bytes,
) -> ImportResult: ...
```
- Builds `ChorusConfig(base_url=..., username=..., password=...)`, awaits `import_user_screen(...)`, maps `JobReturn` → `ImportResult`.
- Catches `ChorusClientError` (config / URL shape problems) and `ChorusAPIException` (HTTP / job-level failures); converts each into an `ImportResult(ok=False, ...)`.
- Does NOT catch generic exceptions — those bubble to the runner for batch-level handling.

`app/services/form_import_runner.py` (~150 lines)
```python
@dataclass(frozen=True)
class FormImportSpec:
    form_name: str       # destination name in Chorus (user-editable)
    xml_path: Path       # absolute path into the analysis output ZIP entry

async def run_batch(
    creds: ImportCredentials,
    specs: list[FormImportSpec],
    on_event: Callable[[dict], Awaitable[None]],
) -> None: ...
```
- Iterates `specs` sequentially (concurrency = 1 — see "Concurrency" below).
- For each: emits `{"type": "form_started", "form_name": ...}`, reads `xml_path` bytes, awaits `import_form`, emits `{"type": "form_ok" | "form_failed", ...}` with the `ImportResult` shape.
- On unexpected exception: emits `{"type": "form_failed", "form_name": ..., "ok": false, "error": <str>}`, continues to the next spec.

### Routes (replaces `chorus_import.py`)

`app/routes/form_import.py`

**`POST /api/sessions/{sid}/form-import/connect`**
- Body: `{ "base_url": str, "username": str, "password": str }`.
- Server-side: `httpx.AsyncClient` performs `GET {base_url}/user` with Basic auth.
- Returns `200 { "ok": true }` on HTTP 200, `200 { "ok": false, "error": <classified-message> }` on HTTP 401 / network errors / URL-shape failures (the connect step renders the error inline; the HTTP status is always 200 unless the request itself is malformed). Does **not** persist creds server-side.

**`WS /api/sessions/{sid}/form-import/stream`**
- First client frame: `{"base_url", "username", "password", "forms": [{"form_name", "form_id"}]}`.
- Server resolves each `form_id` to an XML file inside `app/state/outputs/{sid}.zip` (entry path `xml/{form_id}.xml`).
- Server invokes `run_batch`; relays events to the WS client.
- Server closes the WS with `code=1000` after the last event. Mid-batch network errors flow through `on_event` as `form_failed` so the client keeps row-level state; only batch-level catastrophes (e.g. ZIP file missing) close with non-1000.

### Frontend module layout

**Delete (PR 1):**
- `web/src/panes/ChorusImport.tsx`
- `web/src/hooks/useChorusImport.ts`
- `web/src/api.ts` chorus-specific functions: `chorusLogin`, `chorusLogout`, `listBusinessAreas`, `listEnvironments`, `listWorkTypes`, `startChorusImport`. **Keep `getSummary`** — it's used by `Previewer.tsx` for the Summary tab and by `api.test.ts`.
- `web/src/types.ts` chorus types tied to BA/work-type/queue/process.
- `web/src/__tests__/chorusApi.test.ts`, `web/src/__tests__/ChorusImport.test.tsx`.

**Add (PR 2):**

`web/src/panes/FormImport.tsx` (~250 lines)
- Two-step wizard, `Step = "connect" | "review"`. State machine in `useFormImport`.
- **Connect step:** three inputs (Base URL, Username, Password) + Connect button + inline error. Base URL pre-populated from `localStorage["formImport.baseUrl"]` if set.
- **Review step:** table, one row per form in `analysis.forms`. Columns: Import checkbox (default checked), Form name (editable text input, default derived as below), Status, Warnings. Below table: "Import N forms" button (N = checked rows) + "Back to Connect" link.

`web/src/hooks/useFormImport.ts` (~150 lines)
- State: `{step, creds, rows: {[formId]: RowState}}`.
- `RowState = {form_name, importChecked, status: "idle" | "running" | "ok" | "failed", chorus_code?, description?, warnings?}`.
- `connect()` → POST `/connect`, advances step on `ok: true`.
- `runImport()` → opens WS, sends initial frame with checked rows, listens for per-form events, updates row states, closes on done.
- StrictMode double-mount safe. **Do not copy the `useChat.ts` pattern** — it has no actual guard and is the source of the known "WebSocket error alongside connected" bug noted in the project memory. Instead: mark the previous socket as "stale" on cleanup, and ignore `onerror` / `onclose` events from any non-current socket (compare `event.target` against the live `socketRef.current`). Implement this as a small utility so PR 2 fixes the bug here while leaving the chat bug alone (chat fix is Plan 3c's concern).

**`api.ts` additions:**
- `connectChorus(payload: ConnectPayload): Promise<ConnectResult>`.
- WS URL helper `formImportStreamUrl(sid: string): string`.

### Form-name default derivation

Analysis-output `forms` key shape (observed): `B8A5F5B4-A4CE-4B48-A8F4-B0AFF9B60882-CFDSNASU.CSD`.

```typescript
function defaultFormName(key: string): string {
  // Strip leading "{uuid}-" prefix if present, then strip trailing ".CSD" / ".csd".
  const stripped = key.replace(/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}-/, "");
  return stripped.replace(/\.csd$/i, "").toUpperCase();
}
```
- `B8A5F5B4-...-CFDSNASU.CSD` → `CFDSNASU`.
- If no UUID prefix: `MYFORM.CSD` → `MYFORM`.
- User can override per-row.
- The hidden `form_id` stays as the full key — used to route to the right XML file in the output ZIP.

## Data flow (happy path, one form)

1. User uploads CSD → analysis pipeline (unchanged) produces `app/state/outputs/<sid>.zip` containing `xml/<form-id>.xml` + manifest.
2. User selects "Form import" panel → `FormImport` mounts → reads `analysis.forms` keys, populates table.
3. User fills creds, clicks Connect → backend `GET /user` with Basic auth → 200 → step `review`.
4. User clicks "Import N forms" → frontend opens WS, sends `{creds, forms: [{form_name, form_id}, ...]}`.
5. Backend opens `state/outputs/<sid>.zip`, iterates specs sequentially, calls `import_form(creds, form_name, xml_bytes)` per spec.
6. Per-form events stream over WS → table rows update in real time.
7. Final summary on close (e.g. "5 of 6 imported successfully").

### XML source

Read from `app/state/outputs/<sid>.zip` at WS-stream time, server-side, **one `zipfile.ZipFile` open per batch**. Files are small (<10 KB typical). No pre-extraction needed.

**ZIP entry-name normalization** (the spec's `form_id` field maps to a different filename inside the ZIP):
- The frontend's `form_id` matches the analysis-output key, e.g. `B8A5F5B4-A4CE-4B48-A8F4-B0AFF9B60882-CFDSNASU.CSD` (uppercase UUID, `.CSD` suffix).
- The actual ZIP entry is `xml/b8a5f5b4-a4ce-4b48-a8f4-b0aff9b60882-CFDSNASU.xml` (lowercase UUID prefix, no `.CSD`, `.xml` suffix).
- Server-side normalization, before lookup:
  ```python
  def zip_entry_for(form_id: str) -> str:
      m = re.match(r"^([0-9a-fA-F-]{36})-(.+?)(\.csd)?$", form_id, re.IGNORECASE)
      uuid_lower = m.group(1).lower()
      body = m.group(2)  # the trailing form name, e.g. "CFDSNASU"
      return f"xml/{uuid_lower}-{body}.xml"
  ```
- If the regex fails (no UUID prefix — unlikely but possible with hand-uploaded fixtures), fall back to `xml/{form_id}.xml` after stripping any trailing `.csd`.
- Missing-entry → emit `form_failed` with `error="form artifact missing in analysis output"`, batch continues.

## Error handling

| Failure mode | Detected by | UX |
|---|---|---|
| Bad base_url shape (not REST v1 root) | `ChorusClientError` from helper | Connect step shows the helper's verbatim message — authored for this case. |
| HTTP 401 on `GET /user` bootstrap | connect endpoint catches `httpx.HTTPStatusError` | Connect step shows "Authentication failed" — do not echo the response body. |
| Network error (DNS, timeout) during connect | connect endpoint catches `httpx.RequestError` | Connect step shows "Could not reach Chorus: <type>". |
| Network error mid-import | runner catches, emits `form_failed` | Row → ✗ "Network error". Batch continues. |
| `JobReturn.code != 0` | helper returns failing `JobReturn` | Row → ✗ `<code>: <description>` (verbatim from Chorus). |
| `code == 0` with non-empty `warnings` | helper already filters benign noise per PR #62 | Row → ✓ with a warning badge; click expands the list. |
| Name collision | **unknown** — see "Open questions" | Best case (overwrite): no UI change. Worst case (error): row → ✗ + manual "use new name" prompt. |
| ZIP file missing for `form_id` | runner pre-flight check | WS closes with non-1000 code + error frame; client shows batch-level error. |
| WS disconnect mid-batch | client `onclose` before final event | In-flight rows → ⚠ "Disconnected — status unknown". User re-clicks Import to retry. |

### The benign "Job request node not found!" warning

PR #62's `import_user_screen` already filters this warning server-side (it's an XSLT fallthrough fired by 10+ sibling AJAX jobs as a no-op). **No app code needs to handle it.** Other warnings — if any surface in soak — should be displayed verbatim until we know what they mean.

## Concurrency

`run_batch` runs imports **sequentially** (concurrency = 1):
- Each `import_user_screen` call opens its own `httpx.AsyncClient` and cookie jar (per PR #62), so parallelism is *technically* safe at the helper level.
- But the Chorus portal is single-session-ish in practice — concurrent imports against the same user risk session-state interleaving on the server side, and we have no signal from Chorus about safe concurrency.
- Per-call latency ≈ 1s; batch of 50 forms ≈ 50s. Acceptable for the wizard UX.
- Sequential is also more legible for per-row WS streaming — the user sees rows complete in order.

If batch sizes become large enough to matter (>200 forms), revisit with a bounded `asyncio.Semaphore(2)` after probing parallel behavior in soak.

### Multi-tab concurrent imports

A user can open the same analysis in two browser tabs and click Import in both, kicking off two parallel `run_batch` calls against the same external Chorus instance. With per-row sequential execution inside each batch, the two batches still interleave at the HTTP layer.

**Mitigation (PR 2 ships this):** server-side per-session lock. The WS handler acquires an `asyncio.Lock` keyed by `sid` before invoking `run_batch`; if already held, the second WS closes with `code=1008` + frame `{type: "batch_rejected", reason: "another import is in progress for this session"}`. Lock map lives on `app.state.form_import_locks: dict[str, asyncio.Lock]`. Cleanup-on-shutdown is unnecessary — locks die with the process.

This doesn't protect against two *different* sessions importing the same `form_name` — Chorus is the source of truth there; see name-collision open question.

## Testing strategy

### Delete in PR 1 (~94 backend tests + ~10 frontend removed)

- Backend Plan 4 unit tests: `test_chorus_client.py` (13), `test_chorus_import_runner.py` (10), `test_chorus_mapping.py` (17), `test_chorus_routes.py` (27), `test_chorus_session.py` (17), plus ~7 in `test_db.py` covering `chorus_imports` / `chorus_environments` tables. Total ~91 unit.
- Backend Plan 4 soak: `test_chorus_soak.py` (3 cases, env-gated). Replaced by the new soak below.
- Frontend: `chorusApi.test.ts`, `ChorusImport.test.tsx`, any hook tests for `useChorusImport` (~10 total).

### Add in PR 2

**Backend unit tests (~18):**
- `form_import.py`: happy path (mock `import_user_screen` → assert `ImportResult` mapping); `ChorusClientError` → `ImportResult(ok=False)`; `ChorusAPIException` → `ImportResult(ok=False)`; warnings passthrough; XML bytes propagation.
- `form_import_runner.py`: sequential execution order (assert call order); per-form callback invocation count + payload shape; mid-batch failure doesn't abort siblings; empty `specs` list handles cleanly; ZIP-entry-missing → `form_failed` event (per-row, not batch-level); malformed-XML error from Chorus surfaces verbatim in `form_failed` event.
- `routes/form_import.py`: `/connect` 200-ok and 401 paths (mock httpx); WS protocol — initial-frame validation, event shapes, normal-close on done.

**Frontend tests (~10):**
- `defaultFormName` helper: UUID-prefix strip, `.CSD` suffix strip, uppercase, no-prefix fallback.
- `useFormImport`: state-machine transitions (`connect → review → connect` on back-link); WS-event handling updates row states; StrictMode double-mount safety (stale-socket events from a closed prior socket do not flip connection state — direct test of the guard, since this is the bug-clone-prevention the spec calls out).
- `FormImport.tsx`: render forms from `analysis.forms`; checkbox toggle; row-status rendering; connect-error display; Import button disabled when zero rows checked.

**Soak test (env-gated `CHORUS_SOAK=1`, ~5 cases):**
- One end-to-end import against dev-soak using fixture `10FLDCSD.xml`: connect, import one form, assert `code=0`, no unfiltered warnings.
- **Idempotency probe:** run the same import twice. Record (in test output, not an assertion failure) whether the second call succeeds (overwrite) or errors (collision). Outcome documented in this spec post-soak; if errors, file follow-up issue.
- One intentional failure: connect with a wrong password, assert `/connect` returns `{ok: false}`.
- **Large-batch probe (50 forms):** synthesize 50 minimal-form ZIPs, run a single batch, assert all complete with no timeouts / WS keepalive failures. Records total wall-clock for the spec's "~50s" estimate.
- **Connect-ok-portal-down codification:** mock or rig `GET /user` to succeed while the portal layer 5xxs. Codify the current per-row-failure UX (assert each row → `form_failed`). When the connect step gets a portal-reachability probe (deferred — see open questions), update this test to verify the connect-time block instead.

### Counts after rebuild

- Backend: 176 (current) − ~91 (deleted unit) + 19 (added) = **~104** unit tests + 5 new soak (was 3 soak — different cases).
- Frontend: 60 (current) − ~10 (deleted) + 10 (added — includes stale-socket guard) = **~60** unit tests.

### Out of scope (don't test)

- `chorus-mcp-server` internals — covered by its own 345 tests in `tests/unit/transport/test_portal.py`.
- HTTP / CSRF / multipart shape — the helper's responsibility.

## Open questions (to resolve in implementation / soak)

1. **Name collision behavior.** ~~Does `tcAJAXImportUserScreen` overwrite an existing form with the same `csdName`, or return an error code?~~ **Resolved 2026-05-20 soak (dev-soak):** Chorus overwrites silently. `IDEMPOTENCY: r2.ok=True code=0 desc='The job completed successfully'`. No "Force overwrite" UI affordance needed; no follow-up issue required.
2. **Warnings beyond the benign one.** ~~Are there other XSLT-fallthrough warnings worth filtering?~~ **Partially resolved 2026-05-20 soak:** the one-form-end-to-end soak ran cleanly with zero unfiltered warnings (the only ones that surfaced are filtered server-side by `import_user_screen`). Keep "show verbatim" stance until field reports surface a new pattern.
3. **Connect-step "depth" of validation.** `GET /user` confirms Basic auth but not portal-layer reachability. If users hit "auth works but portal doesn't" in the wild, add a cheap `portal_action` no-op to the connect step. Deferred. (Soak's `test_soak_connect_ok_portal_down_codifies_per_row_failure` codifies the current per-row-failure UX as the expected behavior until then.)
4. **`base_url` shape requirements.** The helper requires the canonical REST v1 root (`/awd/services/v1`). The connect step should validate this and offer a corrected URL in the error message. Confirm exact message wording during implementation. — Still open (no UX change required by this rebuild; deferred to a follow-up).

## Execution strategy

Two PRs, in order:

**PR 1 — Rip Plan 4 (the rip).**
- Delete the files listed above.
- Drop `chorus_*` SQLite tables in `db.py` schema; remove their migration entry.
- Delete corresponding tests (backend + frontend).
- Remove the chorus-import tab from `App.tsx` (or replace with a placeholder).
- README + CHANGELOG note: "Plan 4 (instance-creation wizard) removed pending rebuild against `import_user_screen`."
- Goal: master is clean, "feature missing" but otherwise green. Branch + PR.

**PR 2 — Add form-import (the rebuild).**
- Add `form_import.py`, `form_import_runner.py`, `routes/form_import.py`.
- Add `FormImport.tsx`, `useFormImport.ts`.
- Wire `<FormImport />` into `App.tsx` in the slot vacated by PR 1.
- Add tests (backend + frontend).
- Add soak test, env-gated.
- README + CHANGELOG note for the new flow.
- Soak run before merge; record name-collision outcome in this spec.

Brief feature-missing window on master between PRs is acceptable — this is a single-developer project, no consumers besides the author.

## Spec deltas to the umbrella design

This rebuild invalidates the following sections of [`2026-05-18-agent-app-chorus-csd-analyzer-design.md`](2026-05-18-agent-app-chorus-csd-analyzer-design.md):
- §"Live Chorus import" — the entire BA/work-type/queue framing is wrong. Treat the umbrella spec's Plan 4 sections as historical.
- v4 revision notes about `ChorusClient` / `create_instances` — superseded.
- **v4 revision note "Persist recently-used `base_url`s to a new `chorus_environments` SQLite table"** — explicitly reversed. The new design caches the most-recent base URL in `localStorage["formImport.baseUrl"]` instead. Rationale: server-side persistence of URL history made sense for the multi-step BA/work-type wizard (it depended on env-aware lookups); for a stateless single-input flow, frontend caching is sufficient and removes a write path. If multi-URL history becomes useful later, it can come back as a frontend-only `localStorage` list with zero schema impact.
- PyInstaller `collect_all` list still names `chorus_mcp_server` correctly (transport helpers ship in the same package), so Plan 5 needs no change from this rebuild.
