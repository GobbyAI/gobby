# Fix session-id resolution end-to-end for inner MCP calls

## Context

An agent working in session #3069 reported that inner MCP calls receiving the session's external_id (`b530868c-310f-40ed-a1d5-d8f282d018c6`) failed resolution and forced it to use raw SQLite for task create/close. Observable artifact: task `530d31ae-…` was closed with `closed_in_session_id = NULL`.

**Root cause has two layers, both needed:**

1. **`resolve_session_reference()` never looks at `external_id`.** A valid UUID that maps to `sessions.external_id` but not `sessions.id` raises `ValueError`. Prefix matching has the same blind spot.

2. **Every MCP dispatcher plants an unresolved session id into `SessionContext` and forwards it unresolved to `tool_proxy.call_tool()`.** Four sites, four independent bugs:
   - `src/gobby/mcp_proxy/server.py` lines 225–268 — `GobbyDaemonTools.call_tool()` uses the raw `session_id` to set project context (L225), read `session_manager.get()` (L231), seed `SessionContext` (L237), and call `tool_proxy.call_tool(…, session_id)` (L267). `ToolProxyService._resolve_platform_session_id()` prefers the explicit arg over `ContextVar`, so even fixing the ContextVar leaves the explicit-arg path corrupting workflow checks, tool filters, and synthetic after-tool events.
   - `src/gobby/servers/routes/mcp/endpoints/execution.py` lines 76–129 — `_set_contexts_for_tool_call()` only resolves when `ref.isdigit()` (L83). UUID-shaped refs fall through as `resolved_id = session_id` (L81) and are planted raw at L127. Existing test `tests/servers/routes/mcp_endpoints/test_execution_context.py:143` locks in this misbehavior and will need to flip.
   - `src/gobby/hooks/dispatchers/mcp.py` line 268 — `_sid = arguments.get("session_id", "")` plumbed into `SessionContext(session_id=_sid)`.
   - `src/gobby/workflows/pipeline/handlers.py` lines 39–42 — `pipeline_session_id = context.get("session_id"); set_session_context(SessionContext(session_id=pipeline_session_id))` (pipeline context is not guaranteed to carry a platform UUID).

3. **`_resolve_platform_session_id()` in `tool_proxy.py` L247–258 swallows every exception silently**, so a resolver failure returns the raw unresolved string without a log line. That's why this regressed in production without a test catching it.

Fixing only one of these is a half-measure. Fixing (1) lets downstream defensive resolves succeed; fixing (2) removes the bad input at the source; fixing (3) makes any future regression visible.

**What is NOT a bug (ruled out during investigation):**

- **Duplicate sessions**: 29 external_ids appear twice, but every pair differs legitimately by `session_type`, `machine_id`, or `project_id`. UNIQUE index `(external_id, machine_id, source, project_id)` allows this by design. Zero new dups since 2026-04-19 (commit `87baa66a5`). Legitimate multi-row-per-external-id inside one project is the reason the resolver's new branch needs an explicit ambiguity policy (see change 1).
- **Ghost sessions** (today: 42/47 claude, 14/14 qwen, 28/28 gemini, 7/16 codex empty `expired` rows): caused by the daemon being repeatedly SIGTERMed by `ppid=1` (launchd) — visible in `~/.gobby/logs/gobby.log` at 00:07 and 00:30 CDT — which forces CLI adapters to re-fire SESSION_START on restart. Orthogonal to this bug; file separately.
- **Task audit NULLs elsewhere**: `created_in_session_id` and `closed_in_session_id` are the only `*_in_session_id` audit columns that task-lifecycle storage actually writes. `escalate_task` / `de_escalate_task` / `mark_task_review_approved` / `mark_task_needs_review` at `src/gobby/storage/tasks/_transitions.py:128,157,233` do not persist any session column. Previous version of this plan incorrectly listed two of those as silent-NULL sites.

## Scope

### Change 1 — `src/gobby/storage/session_resolution.py`: external_id fallback with explicit ambiguity policy

New resolution order:

- **`#N` / `N`** — unchanged (still requires `project_id`).
- **Full valid UUID**:
  1. Try `SELECT id FROM sessions WHERE id = ?`.
  2. If no row, fall back to `SELECT id FROM sessions WHERE external_id = ?` scoped by `project_id` when provided.
  3. Multi-row policy: if the `external_id` query returns >1 row (legal per the `(external_id, machine_id, source, project_id)` UNIQUE index — same external_id can coexist across machine/source within one project), raise `ValueError("Ambiguous session reference '…' — external_id matches N sessions in project (ids: …). Pass the primary-key UUID to disambiguate.")`. Deterministic failure is strictly better than silent nondeterminism.
  4. If `project_id is None` and external_id match returns >1 row across projects, same raise.
  5. Return the primary key `id` (never the external_id), preserving the contract that `SessionContext.session_id` is always a platform UUID.
- **Prefix**:
  1. Try `WHERE id LIKE 'ref%' LIMIT 5`.
  2. If zero `id` matches, try `WHERE external_id LIKE 'ref%'` scoped by `project_id`.
  3. Any `id` match wins (primary-key-first for backward compatibility).
  4. Multiple `external_id` prefix matches → `ValueError("Ambiguous …")`.

No schema change — `idx_sessions_external_id` already exists. Query via `DatabaseProtocol` consistent with the rest of the file; do not pull in `LocalSessionManager.find_by_external_id*` (those require `machine_id` the resolver doesn't have).

### Change 2 — Shared helper + single resolve-and-propagate path in each dispatcher

**Extract a shared helper first**, then rewrite each of the four dispatchers to call it. Four sites reinvent the same logic poorly today — both for session resolution and for project-context derivation — and that drift is exactly what produced this bug. The helper centralizes **both** ContextVars behind one priority rule; dispatchers shrink to input-passing and dispatcher-specific error policy.

Add to `src/gobby/utils/session_context.py` (alongside `SessionContext` / `set_session_context`):

```python
@dataclass
class SeededContextTokens:
    session_token: contextvars.Token[SessionContext | None] | None = None
    project_token: contextvars.Token[dict[str, Any] | None] | None = None
    resolved_session_id: str | None = None
    resolved_project_id: str | None = None

def resolve_and_seed_contexts(
    session_ref: str | None,
    session_manager: "LocalSessionManager | None",
    *,
    project_ref: str | None = None,            # UUID or name; helper canonicalizes
    project_ref_is_fallback: bool = False,     # True for HTTP header semantics (see modes below)
    db: "DatabaseProtocol | None" = None,      # project lookup; None triggers minimal fallback path
) -> SeededContextTokens:
    """Resolve session and project refs, set SessionContext and project context, return tokens.

    `project_ref` is always used for session-scoping. The mode flag only affects project
    *context* precedence when a session also resolves:

    * override mode (default, `project_ref_is_fallback=False`) — explicit caller intent:
        project_ref > session-derived project.
        Use when the caller passed an explicit `project_id` param to override (e.g. server.py's
        cross-project tool calls).
    * fallback mode (`project_ref_is_fallback=True`) — bootstrap hint only:
        session-derived > project_ref.
        Use when project_ref is a bootstrap hint, not an override (e.g. execution.py's
        `x-gobby-project-id` header — preserves the current "session's own project wins" contract).

    Both modes fall through to `project_ref` when session resolution fails.

    On `db is None` with a `project_ref` that cannot be enriched, emit a minimal project context
    of `{"id": project_ref}` — preserves the existing HTTP minimal fallback at execution.py:164.

    On `project_ref` unresolvable: `resolved_project_id` is None — callers decide whether that's
    a hard error (server.py returns "project not found") or silent (no other dispatcher does).

    On session_ref unresolvable: SessionContext is not set; project context is set per above.
    """

def reset_seeded_contexts(tokens: SeededContextTokens) -> None:
    """Reset both tokens if set. Safe for partially-populated or empty tokens."""
```

Helper responsibilities (one place, tested once):
1. Canonicalize `project_ref` via `LocalProjectManager.resolve_ref()` when `db` is available (handles UUID-or-name). When `db` is `None`, accept `project_ref` as-is (treated as UUID for the minimal-fallback path below).
2. `session_manager.resolve_session_reference(session_ref, canonical_project_id)` scoped by the canonical project id. On `ValueError`: `logger.warning(...)`, skip SessionContext, continue to project-only step.
3. On successful session resolve: `session = session_manager.get(resolved_id)`, `conversation_id = session.external_id`, `set_session_context(SessionContext(session_id=resolved_id, conversation_id=conversation_id))`.
4. Project context selection, in this order:
   - **override mode + session resolved + `canonical_project_id`**: `set_project_context_from_ref(canonical_project_id, db)`.
   - **override mode + session resolved + no project_ref**: `set_project_context_from_session(resolved_id, session_manager, db)`.
   - **fallback mode + session resolved**: try `set_project_context_from_session(resolved_id, session_manager, db)` first; on exception or `None` return, fall through to `set_project_context_from_ref(canonical_project_id, db)` if available.
   - **session unresolved + `canonical_project_id`**: `set_project_context_from_ref(canonical_project_id, db)`; on exception or `db is None`, fall back to minimal `set_project_context({"id": canonical_project_id})`.
   - **otherwise**: no project_token.

Every dispatcher must propagate `tokens.resolved_session_id` (not the raw ref) to `tool_proxy.call_tool()` / `get_tool_schema()`. `ToolProxyService._resolve_platform_session_id()` prefers the explicit `session_id` arg over the ContextVar — leaving it raw would re-poison workflow checks, tool filters, and synthetic after-tool events even after the ContextVar is clean.

**2a. `src/gobby/mcp_proxy/server.py` `GobbyDaemonTools.call_tool()` (lines 180–280)**

- Preserve the dispatcher-specific **infrastructure** precondition at L214: if `project_id` was supplied but no session_manager/db is available, return the distinct "no database available" error *before* calling the helper. Do not collapse this case into "project not found" — it's a different failure mode (infrastructure vs. user input) and conflating them makes diagnosis harder.

  ```python
  if project_id and (self._session_manager is None or self._session_manager.db is None):
      return CallToolResult(isError=True, content=[TextContent(type="text", text="Error: project_id provided but no database available to resolve it.")])
  ```

- Replace the remaining L198–239 block (explicit `project_id` handling + session-derived project + SessionContext) with one helper call in **override mode** (default — explicit `project_id` is meant to override the session's project, per the current docstring at L188–191):

  ```python
  tokens = resolve_and_seed_contexts(
      session_ref=session_id,
      session_manager=self._session_manager,
      project_ref=project_id,          # UUID or name — helper canonicalizes
      # project_ref_is_fallback=False  # default; explicit project_id overrides session project
      db=(self._session_manager.db if self._session_manager else None),
  )
  ```

- Preserve the dispatcher-specific **user-input** error policy: if the caller passed `project_id` but the helper couldn't resolve it, return the existing "not found" error:

  ```python
  if project_id and tokens.resolved_project_id is None:
      return CallToolResult(isError=True, content=[TextContent(type="text", text=f"Error: project_id {project_id!r} not found. Use a valid project UUID or name.")])
  ```

- L267–268 `tool_proxy.call_tool(…, session_id=tokens.resolved_session_id)` — propagate resolved UUID, not raw ref.
- L270–278 finally-block becomes `reset_seeded_contexts(tokens)`.

**2b. `src/gobby/servers/routes/mcp/endpoints/execution.py` `_set_contexts_for_tool_call()` (lines 66–167)**

- Harvest refs as today: `session_id = arguments.get("session_id") or header_session_id` (L68–73) and `project_id_header = request.headers.get("x-gobby-project-id")`.
- **Preserve the project_id bootstrap branch from `x-gobby-session-id`** (currently L84–106): when both (a) the incoming `session_id` is `#N`/numeric AND (b) `x-gobby-project-id` is missing, try resolving `header_session_id` to a UUID and read that session's `project_id`. Keep this as a dispatcher-local pre-step that feeds into the helper:

  ```python
  canonical_project_ref = project_id_header
  if not canonical_project_ref and header_session_id and session_id and session_id.lstrip("#").isdigit():
      try:
          bootstrap_id = resolve_session_reference(server.session_manager.db, header_session_id)
          bootstrap_session = server.session_manager.get(bootstrap_id)
          if bootstrap_session:
              canonical_project_ref = bootstrap_session.project_id
      except (ValueError, Exception) as e:
          logger.debug(f"HTTP project bootstrap from header session {header_session_id!r} failed: {e}")
  ```

  This is HTTP-specific scope-bootstrapping logic and does not belong in the helper. After Change 1, `resolve_session_reference(db, header_session_id)` (no project scope) can handle header values that are external_id UUIDs too.
- Replace the remaining L76–166 body (the resolve-and-set-contexts block + the header-only project fallback) with the single helper call in **fallback mode** — the header is a bootstrap hint, not an override, so a resolved session's own project should win:

  ```python
  tokens = resolve_and_seed_contexts(
      session_ref=session_id,
      session_manager=server.session_manager,
      project_ref=canonical_project_ref,   # x-gobby-project-id header (after the #N bootstrap above)
      project_ref_is_fallback=True,        # preserves current HTTP semantics
      db=(server.session_manager.db if server.session_manager else None),
  )
  ```

- The helper's mode-aware priority rule subsumes both remaining HTTP branches: session-derived project (current L131–140) wins when session resolves, and the header-only fallback (current L142–165, including the minimal `{"id": project_id}` fallback at L164–165 when no DB is available) kicks in when session fails or session-derived enrichment fails.
- Shim the returned `SeededContextTokens` into the caller's existing `_ContextTokens` shape (or rename `_ContextTokens` to alias `SeededContextTokens`). `_reset_context()` at L170–175 becomes `reset_seeded_contexts(tokens)`.
- `if ref.isdigit():` format-detection gate at L83 is gone — the resolver handles all ref formats uniformly.

**2c. `src/gobby/hooks/dispatchers/mcp.py` `_call()` (lines ~245–333)**

- Fetch the session_manager via the existing proxy-discovery path at L271–274 (`proxy = _get_proxy(); mgr = proxy._mcp_manager; session_manager = getattr(mgr, "session_manager", None)`), then replace the manual L264–287 block with:

  ```python
  tokens = resolve_and_seed_contexts(
      session_ref=_sid,
      session_manager=session_manager,
      project_ref=None,  # no independent project ref on this path
      db=(session_manager.db if session_manager else None),
  )
  ```
- L311 `await proxy.call_tool(...)` — pass `session_id=tokens.resolved_session_id`.
- `finally` block at L327–333 becomes `reset_seeded_contexts(tokens)`.
- Non-obvious subtlety: L235 injects `arguments["session_id"] = event.metadata.get("_platform_session_id", "")` only when the caller didn't pass one. That path is fine because `_platform_session_id` is pre-resolved by `SessionLookupService.resolve()`. The new regression test must therefore bypass the default injection and pass `arguments={"session_id": <external_id>}` explicitly — see Change 5.

**2d. `src/gobby/workflows/pipeline/handlers.py` `execute_mcp_step()` (lines 37–79)**

- Replace the manual `set_session_context` + `set_project_context_from_session` block at L37–56 with:
  ```python
  session_manager = getattr(tool_proxy._mcp_manager, "session_manager", None) if hasattr(tool_proxy, "_mcp_manager") else None
  tokens = resolve_and_seed_contexts(
      session_ref=pipeline_session_id,
      session_manager=session_manager,
      project_ref=None,
      db=(session_manager.db if session_manager else None),
  )
  ```
- L64 `tool_proxy.get_tool_schema(…, session_id=tokens.resolved_session_id)` and L78 `tool_proxy.call_tool(…, session_id=tokens.resolved_session_id)` — propagate the resolved UUID.
- `finally` block L80–86 becomes `reset_seeded_contexts(tokens)`.

### Change 3 — Narrow the silent catch in `ToolProxyService._resolve_platform_session_id()` (`src/gobby/mcp_proxy/services/tool_proxy.py` L247–258)

- Catch `ValueError` only (not bare `Exception`). Log `logger.warning("Could not resolve session reference %r (project_id=%s): %s", requested_session_id, project_id, exc)`.
- Preserve the current return-unresolved-string fallback so existing best-effort call sites keep working — visibility is what fixes the bug; the fallback stays so we don't break paths that rely on it.
- Let non-`ValueError` exceptions propagate. DB or config errors should surface, not masquerade as missing sessions.

### Change 4 — Task-lifecycle session-context guards

**4a. `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` lines 61–124, 311 — close_task NULL guard**

Close is the only task-lifecycle tool that persists a `*_in_session_id` audit column and currently lacks a guard — 72% of historical closed tasks have NULL `closed_in_session_id`, including rows from today.

- If `get_current_session_id()` returns `None` at L63, fall back to the task's existing `claimed_by_session_id` (already available on the fetched `task` object at L72). `logger.warning("close_task: no session context; falling back to task.claimed_by_session_id=%s", task.claimed_by_session_id)`.
- If neither ContextVar nor `claimed_by_session_id` is set, return `{"error": "no_session_context", "message": "close_task requires an active session context or a previously-claimed task"}`. Audit-trail fields should not be silently NULL-ed.
- Keep the existing `ctx.resolve_session_id(session_id)` call at L122; after change 1 it succeeds on external_ids too.

**4b. `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py` L146 (escalate_task) and L480 (de_escalate_task) — add early session-context guards**

These two tools collect `session_id = get_current_session_id()` but never persist it (the storage transitions at `_transitions.py:128,157` don't write session columns). Nevertheless, calling them without a session context is almost certainly a bug in the caller — silently proceeding conceals it.

- Add the same early guard used by sibling status-transition tools at L238 and L360:
  ```python
  if not session_id:
      return {"error": "No session context available. Ensure session_id is set."}
  ```
  Insert immediately after the `session_id = get_current_session_id()` line in both functions.
- This brings all task-lifecycle MCP tools to a consistent contract: either an active session exists, or the tool returns an explicit no_session_context error. No more collect-and-discard.

### Change 5 — Tests

**Helper** (`tests/utils/test_session_context.py` — add alongside existing SessionContext tests, or create if absent). Cover both modes, the minimal fallback, and every branch:

- `test_resolve_and_seed_contexts_external_id_ref_resolves_to_platform_uuid` — primary regression: pass an external_id that's a full UUID, get the platform id, SessionContext holds the platform id, conversation_id is the external_id.
- `test_resolve_and_seed_contexts_session_only_derives_project_from_session` — `project_ref=None`, session resolves → project context derived from session.

*Override mode (default, `project_ref_is_fallback=False`)*:

- `test_override_mode_project_ref_uuid_beats_session_derived_project` — mirrors server.py's cross-project use case: session in project A, `project_ref=<UUID of project B>` → project context is B, session context is the resolved session.
- `test_override_mode_project_ref_name_canonicalized` — `project_ref="my-project"` (a name) is resolved to the canonical UUID before scoping + setting project context.
- `test_override_mode_project_ref_unresolvable_returns_none_project_id` — helper does not raise; `resolved_project_id is None`; caller decides whether to treat this as a hard error (server.py returns "project not found").

*Fallback mode (`project_ref_is_fallback=True`, HTTP header semantics)*:

- `test_fallback_mode_session_project_wins_over_project_ref` — session resolves to a session in project A; `project_ref=<UUID of project B>` is *ignored* for project context; project is A, session context is the resolved session. Explicitly verifies the regression Codex flagged.
- `test_fallback_mode_session_unresolvable_project_ref_sets_project` — session fails, `project_ref` succeeds → project context is set from project_ref; no SessionContext.
- `test_fallback_mode_session_derivation_fails_falls_through_to_project_ref` — session resolves but `set_project_context_from_session` raises/returns None → helper falls through to `project_ref`-derived project context (mirrors current execution.py L138→L142 fallthrough).

*Minimal / no-DB fallback*:

- `test_minimal_fallback_without_db_sets_id_only_project_context` — `db=None`, `project_ref=<UUID>`: helper sets `{"id": project_ref}` as project context (mirrors current execution.py L164–165). Works in both modes.
- `test_minimal_fallback_with_db_but_enrichment_failure` — `db` present but `set_project_context_from_ref` throws; helper falls back to minimal `{"id": canonical_project_id}` context.

*Other*:

- `test_resolve_and_seed_contexts_both_unresolvable_returns_empty_tokens` — no ContextVars set; `reset_seeded_contexts` is a no-op.
- `test_resolve_and_seed_contexts_valueerror_on_session_logs_warning` — ambiguous external_id surfaces as a warning, returns empty session token (project context still handled per priority).
- `test_reset_seeded_contexts_safe_on_empty_and_partial_tokens` — reset with session_token=None and/or project_token=None is a no-op.

**Resolver** (`tests/storage/test_storage_sessions.py`):

- `test_resolve_reference_by_external_id_full_uuid` — seed `id=<uuid_a>, external_id=<uuid_b>`; `resolve_session_reference(db, str(uuid_b), project_id)` returns `str(uuid_a)`.
- `test_resolve_reference_by_external_id_full_uuid_no_project_scope` — same seed, `project_id=None`.
- `test_resolve_reference_by_external_id_prefix` — prefix of `external_id` resolves to platform `id`.
- `test_resolve_reference_prefers_id_match_over_external_id_match` — `id` prefix wins.
- `test_resolve_reference_ambiguous_external_id_in_project_raises` — two rows, same project, same external_id, different source → `ValueError` listing candidate ids.
- `test_resolve_reference_external_id_cross_project_no_scope_ambiguous_raises` — same external_id across two projects, `project_id=None` → raises.
- `test_resolve_reference_ambiguous_external_id_prefix_raises`.
- `test_resolve_reference_unknown_ref_still_raises` — guard the not-found path.

**Dispatcher — HTTP** (`tests/servers/routes/mcp_endpoints/test_execution_context.py`):

- **Flip the existing test at L143** that asserts `resolved_id == session_id` for UUID-shaped refs. New assertion: a UUID that is an `external_id` resolves to the platform id.
- Review L45 and surrounding cases for the same lock-in.
- `test_set_contexts_unresolvable_uuid_does_not_plant_session_context` — warning logged, `tokens.session is None`.

**Dispatcher — GobbyDaemonTools** (`tests/mcp_proxy/test_server.py` or closest existing):

- `test_call_tool_resolves_external_id_to_platform_uuid` — `SessionContext.session_id` inside the tool equals the platform key, and `tool_proxy.call_tool` receives the same resolved id (not the raw external_id).
- `test_call_tool_skips_session_context_when_unresolvable` — warning logged, no SessionContext.

**Dispatcher — hooks/dispatchers/mcp.py** (actual file: `tests/hooks/test_dispatch_mcp_calls.py`):

- `test_dispatch_resolves_external_id_before_setting_session_context` — **must pass `arguments={"session_id": <external_id_uuid>}` explicitly**. The default `_make_event()` helper (at `tests/hooks/test_dispatch_mcp_calls.py:32`) seeds `event.metadata["_platform_session_id"]` which the dispatcher injects at L235 as an already-resolved platform id; relying on the default would test the happy path and miss this bug entirely.
- `test_dispatch_unresolvable_session_id_skips_set_session_context` — also using an explicit caller-supplied `session_id` argument; warning logged; neither SessionContext nor project ContextVar planted.

**Dispatcher — workflows/pipeline/handlers.py** (actual file: `tests/workflows/test_mcp_step.py`):

- `test_execute_mcp_step_resolves_external_id_before_session_context` — near the existing cases around L176.
- `test_execute_mcp_step_unresolvable_session_id_skips_set_session_context`.
- Fixture upgrade: the `mock_tool_proxy` fixture at `tests/workflows/test_mcp_step.py:168` is currently `AsyncMock()` with no `_mcp_manager.session_manager`. Extend it with a stub `_mcp_manager.session_manager.resolve_session_reference(...)` and `.get(...)` so the new tests exercise the real resolution branch. Keep existing tests passing by making the stub default to a no-op.

**Silent catch** (actual files: `tests/mcp_proxy/services/test_tool_proxy_coverage.py` near L232 and `tests/mcp_proxy/services/test_tool_proxy_validation.py` near L197 — put the new cases in whichever already covers `_resolve_platform_session_id`):

- `test_resolve_platform_session_id_logs_warning_on_valueerror` — ValueError from resolver → warning + return unresolved string.
- `test_resolve_platform_session_id_propagates_non_valueerror` — DB error propagates, not swallowed.

**close_task NULL guard** (`tests/mcp_proxy/tools/tasks/test_lifecycle_close.py`):

- `test_close_task_without_session_context_falls_back_to_claimed_by_session_id`.
- `test_close_task_without_session_context_or_claimed_by_errors`.

**escalate_task / de_escalate_task guards** (`tests/mcp_proxy/tools/tasks/test_lifecycle_status.py` or closest existing):

- `test_escalate_task_without_session_context_errors` — no SessionContext → `{"error": "No session context available. Ensure session_id is set."}`, task state unchanged.
- `test_de_escalate_task_without_session_context_errors` — same expectation.

## Follow-ups (out of scope for this task)

- Ghost-session investigation: why is launchd SIGTERMing the daemon repeatedly? Should SESSION_START register eagerly or only on first user message? Should empty `expired` sessions be GC'd?

## Verification

1. **Unit + targeted**: `uv run pytest tests/utils/test_session_context.py tests/storage/test_storage_sessions.py tests/servers/routes/mcp_endpoints/test_execution_context.py tests/mcp_proxy/ tests/hooks/test_dispatch_mcp_calls.py tests/workflows/test_mcp_step.py tests/mcp_proxy/services/test_tool_proxy_coverage.py tests/mcp_proxy/services/test_tool_proxy_validation.py tests/mcp_proxy/tools/tasks/test_lifecycle_close.py tests/mcp_proxy/tools/tasks/test_lifecycle_status.py -v`.
2. **Typecheck**: `uv run mypy src/gobby/utils/session_context.py src/gobby/storage/session_resolution.py src/gobby/mcp_proxy/server.py src/gobby/mcp_proxy/services/tool_proxy.py src/gobby/servers/routes/mcp/endpoints/execution.py src/gobby/hooks/dispatchers/mcp.py src/gobby/workflows/pipeline/handlers.py src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`.
3. **Lint**: `uv run ruff check src/ --fix && uv run ruff format src/`.
4. **Reproduce #3069 symptom**: seed a session where `id != external_id`; close a task through `GobbyDaemonTools.call_tool` passing the external_id; post-fix `closed_in_session_id` is the platform UUID, not NULL, not the external_id.
5. **End-to-end (daemon running)**: from an agent-spawned inner MCP call, `create_task` / `close_task` with the parent CLI's external_id; verify audit columns store platform UUIDs.
6. **Log audit**: `grep -E "could not resolve session ref|Could not resolve session reference|close_task: no session context" ~/.gobby/logs/gobby.log` is empty under normal traffic; any hit is actionable instead of invisible.
