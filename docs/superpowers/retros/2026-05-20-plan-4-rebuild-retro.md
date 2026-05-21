# Plan 4 Rebuild — Retro

**Date:** 2026-05-20
**Scope:** Ripping the wrong-abstraction Plan 4 wizard and rebuilding it against `chorus_mcp_server.transport.import_user_screen` (PR #62). Shipped as app-repo PRs #3 (rip, squash `6f96856`) + #5 (rebuild, squash `2874dde`).
**Context:** Single-session autonomous flow from "what's next?" to two merged PRs.

This retro is intentionally short — captures the patterns and gotchas worth carrying forward, not every implementation detail (the spec and plan capture those). Future-me reading this should be able to apply the lessons to the next rip-and-rebuild without re-deriving them.

## What worked

### 1. Manual reality check before claiming "done"

The original Plan 4 (BA/work-type/queue instance creation) passed all its unit tests and a soak run against dev-soak. It was "shipped." Then a manual UI walkthrough on 2026-05-20 surfaced that **it solved the wrong problem** — forms are global Chorus schema objects, not BA-scoped data instances.

**Carry-forward:** unit tests + mocked soak don't prove an integration matches user intent. Walk through the real UI against the real backend at least once before declaring victory. The cost (one session) is trivial compared to discovering the wrong-abstraction later.

### 2. Co-planning with a different model family caught real spec bugs

Gemini 2.5 Pro reviewed the rebuild spec before any code was written. Three CRITICAL/IMPORTANT findings landed:
- Spec omitted `app/main.py` cleanup (imports + lifespan + router include) for PR 1.
- XML path normalization was wrong: spec said `xml/{form_id}.xml` but the actual ZIP entry has lowercased UUID + no `.CSD`.
- `getSummary` was listed for deletion but `Previewer.tsx` also uses it.

All three would have been bugs at implementation time. The cost was ~10 minutes of review and ~30 minutes of spec edits. The cost of debugging the three live bugs would have been ~hours each.

**Carry-forward:** for non-trivial changes, co-plan with a different model before writing code. The `co-plan` skill exists for exactly this. Use it.

### 3. Subagent-driven implementation surfaced more plan bugs

Fresh-context subagents executing TDD tasks caught plan errors that I (the plan author) had missed because I had broader context that hid the gap:

| Task | Bug the subagent caught |
|---|---|
| A2 | Two test files imported from services A2 deleted — would have broken pytest collection between A2 and A3. Folded their deletion forward. |
| A4 | Plan's "canonical content" for `main.py` dropped the trailing `app = build_app()` — would have broken `uvicorn app.main:app`. |
| A5 | Plan's new table-count test didn't account for SQLite auto-creating `sqlite_sequence` from `AUTOINCREMENT`. Added `WHERE name NOT LIKE 'sqlite_%'`. |
| B10 | Plan's `FakeWS` test double didn't set `event.target` on dispatched events — broke the `ignoreIfStale` guard test silently. Fix: `Object.defineProperty(ev, "target", { value: this })`. |
| B11 | Plan's component used `<input type="url" required>` but tests passed `"x"` as `base_url` — jsdom blocks submit on invalid URLs. Relaxed to `type="text"`. |

**Carry-forward:** a thorough spec/plan still has bugs you can't see from the planning altitude. Subagents with fresh context catch them. Don't treat subagent escalations as friction — they're the value.

### 4. Soak run answered an open spec question in seconds

The spec had an open question: *"Does `tcAJAXImportUserScreen` overwrite an existing form, or error on duplicate `csdName`?"* We almost designed a "Force overwrite" UI affordance against the error case. The soak's idempotency probe ran the same import twice — Chorus returned `code=0` on the second try. **Silent overwrite.** No UI affordance needed.

**Carry-forward:** when a spec question is empirically answerable, include the probe in the soak suite explicitly. Five seconds of real execution > hours of design hedging.

### 5. Cross-family review fallback ladder worked as designed

The `ship` skill's review step walked the ladder:
- **Codex** → Windows sandbox bug (`CreateProcessAsUserW failed: 5`) — couldn't run `git diff`.
- **opencode** → its own sandbox auto-rejected `D:\*` access mid-investigation.
- **pr-reviewer-consensus** (Gemini + Kimi + DeepSeek via OpenRouter, ≥2-model agreement) → **worked**, returned 0 findings.

Without the ladder we'd either have skipped cross-family review (weakening the gate) or blocked on manual setup. The fallback design is load-bearing.

**Carry-forward:** the ladder is in the `ship` skill. Trust it. If the first option fails, fall through don't bypass.

### 6. Rip-then-rebuild beats incremental refactor for wrong-abstraction pivots

Two PRs (rip + rebuild) made the "before" and "after" obvious in git history and in PR diffs. A reviewer can see "deleted ~5000 lines of wrong-shape code" and "added ~500 lines of right-shape code" as two independent stories. Incremental refactor would have left phantom abstractions (BA/work-type/queue concepts hanging around) and a much harder-to-review diff.

**Carry-forward:** when an abstraction is fundamentally wrong (not just buggy), delete cleanly before rebuilding. The cost of a brief "feature-missing" window on master is low; the readability gain is high.

## Gotchas hit

### Stacked-PR auto-close

`gh pr merge 3 --squash --delete-branch` succeeded. GitHub then auto-closed PR #4 (which was stacked on `rip-plan-4-instance-wizard`) because its base branch was deleted. **You cannot reopen a closed PR whose base branch no longer exists, and you cannot change the base of a closed PR.** Catch-22.

**Recovery:** rebase the stacked branch onto master, force-push, open a fresh PR. The discussion history on the old PR is preserved as a closed reference but doesn't carry forward.

**Avoidance going forward:**
- Option A: rebase the stacked branch onto master BEFORE merging the base PR, then merge the base, then merge the (now-against-master) stacked PR. Both PRs end up against master and squash-merge sequentially.
- Option B: open both PRs against master from the start; merge sequentially in dependency order. Trivially avoids the issue.
- Don't use `--delete-branch` on the base PR while a stacked PR points at it.

### Windows venv churn

`uv run pytest` intermittently failed with `error: failed to remove directory ... \chorus_mcp_server-0.5.0.dist-info\licenses: Access is denied. (os error 5)` due to Windows file locking on stale dist-info dirs. Hit this at A1, recurred sporadically.

**Workaround that worked:** `.venv/Scripts/python.exe -m pytest ...` directly, bypassing uv's sync step. This is what subagents used throughout the rebuild.

**Avoidance:** if you see the error once, switch the whole task list to `.venv/Scripts/python.exe -m pytest`. uv self-cleans eventually (a later `uv pip install --force-reinstall` of the affected package clears it), but the timing is unpredictable.

### Venv broke between A1 and B2

Between A1 (baseline) and B2 (first task to import from the rebuilt `chorus_mcp_server.transport.portal`), the venv's editable install of `chorus_mcp_server` somehow lost its `.pth` finder file. Symptom: the `chorus_mcp_server/` dir in site-packages became a hollow namespace package with only empty subdirs.

Root cause: likely an intermediate `uv run` triggered a partial sync that uninstalled but couldn't reinstall (due to the dist-info file-lock issue above).

**Recovery:** `uv pip install -e D:/chorus-mcp-server --no-deps` force-reinstalled the editable. Also had to `git -C D:/chorus-mcp-server pull` first since the local clone was 3 commits behind origin/main (missing PR #62).

**Carry-forward:** when depending on an editable sibling-repo install, verify the sibling is at the expected SHA at the start of a session that depends on new functionality. Don't assume the venv resolves correctly.

### Test gates that don't gate

Two regressions slipped past the in-flight test gates and were only caught later:

1. **B6→B13:** When B9's MINOR-fix changed `FormImportSpec` to require `form_id`, the subagent updated `test_form_import_runner.py` and `test_form_import_routes.py` but missed `test_form_import_soak.py` (env-gated, not exercised in the normal pytest run). User caught it when running the soak suite for the first time.
2. **A6 backend smoke test** ran fine but didn't catch the fact that A4's commit dropped the module-level `app = build_app()`. The smoke test imported `build_app` and called it, so `app.main:app` brokenness wasn't surfaced. Caught by the implementer's self-review concern flag.

**Carry-forward:** when a code change alters a type signature, grep for ALL usages including env-gated tests before committing. When writing smoke tests, exercise the production entry path (`app.main:app`), not just the factory (`build_app()`).

## Anti-patterns avoided

- **Didn't try to incrementally migrate the wrong wizard into the right one** — would have left phantom abstractions and an unreadable diff. Rip-then-rebuild was cleaner.
- **Didn't skip the soak run "because unit tests pass"** — would have shipped a "Force overwrite" UI for an idempotent operation.
- **Didn't skip cross-family review "because self-review found everything"** — turned out to be true this time, but unknowable in advance. Running it costs ~25 min and a small amount of OpenRouter credit.
- **Didn't paper over the stacked-PR mishap with `git branch -D` + force-push to master** — properly rebased, opened a fresh PR, kept history clean.

## What I'd do differently

- **Open stacked PRs against master from the start.** Sequential merging is simpler than stacked-base management.
- **Add a "shape-change ripple grep" step to subagent prompts when modifying a shared type.** "If you change `FormImportSpec`, grep for all `FormImportSpec(...)` constructions across `tests/` (including env-gated soak files) and update them in the same commit."
- **Pre-clear the Windows venv churn at session start.** A one-time `uv pip install --force-reinstall -e D:/<sibling> --no-deps` for each editable sibling-repo dep, before any test runs. Removes the intermittent file-lock failures.
- **Note in the plan when an existing memory file needs updating post-merge.** Memory updates are easy to forget in the merge afterglow.

## Reusable artifacts

These survived the rebuild and are reusable on future Plan-3c-style work:
- **`web/src/staleSocket.ts` (`ignoreIfStale`)** — the React 18 StrictMode WS-event filter. Plan 3c's chat agent should adopt it to fix the existing chat "WebSocket error alongside connected" bug noted in [`project_app_plans_status`](../../../../../Users/chris/.claude/projects/d--agent-harness-chorus-csd-analyzer/memory/project_app_plans_status.md).
- **`app.state.form_import_inflight` (per-sid in-flight set)** — atomic check-and-add pattern for "reject if already in progress, accept otherwise." Cleaner than `asyncio.Lock` for non-queueing semantics. Reusable for any per-session serialization where queueing would surprise the user.
- **The `(form_id, form_name, zip_entry)` wire contract** — client sends `form_id`, server computes `zip_entry` (filesystem path mapping stays server-side). Pattern generalizes: clients send identifiers, servers translate to internal locations.
- **The replication script at `C:\Users\chris\Downloads\chorus_form_import_replication.py`** — minimal-dep diagnostic for `tcAJAXImportUserScreen`. Useful if `import_user_screen` ever misbehaves and you want to bisect at the HTTP layer.

## Numbers

- **Implementation:** 23 plan tasks, ~25 commits (across both PRs), ~6 hours wall-clock from "begin" to "merged."
- **Code delta:** ~5000 lines + ~91 backend tests + ~10 frontend tests removed (rip); ~500 lines + 19 backend unit + 5 soak + 10 frontend added (rebuild). Net: shipped a smaller, more correct implementation.
- **Plan bugs surfaced by execution:** 6 (counted above).
- **Self-review findings (Claude subagent):** 3 IMPORTANT + 5 MINOR, all addressed.
- **Cross-family review findings (Gemini + Kimi + DeepSeek consensus):** 0.
- **Soak findings (real Chorus dev-soak):** 4/5 PASSED first run; 1 self-inflicted test regression caught and fixed.
- **PR mishap recovery cost:** ~5 minutes (rebase + push + reopen-fail + new-PR + merge).
