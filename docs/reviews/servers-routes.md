# Review: servers/routes

- **Scope:** `src/gobby/servers/routes/` (HTTP API routes, ~60 files / ~20,190 lines), split across 6 parallel reviewers by route domain (tasks/build/cron · mcp/ · sessions/agents · files/git/content · admin/config/auth · pipelines/workflows/llm/voice). Synthesized and Blocker-verified against source.
- **Reviewer:** Claude (Fable 5) — 6 general-purpose review agents + synthesizer verification
- **Commit / branch:** `0.5.0` @ 530a61ff3
- **Summary:** 6 Blocker · 27 Important · 23 Nit — the route layer's load-bearing problem is **synchronous DB / crypto / subprocess work on the event loop**, present in nearly every file; on top of that sit several silent-data-corruption bugs (wrong SQL dialect, project-scope `""`, token replay, duplicate-then-delete) and a cluster of auth-boundary gaps that matter only when the operator enables auth or binds off-localhost.

> Verification note: all 6 Blockers were re-read directly against source (auth middleware → `SecretStore` PBKDF2 chain; `_build_filters` SQLite SQL vs the Postgres hub; `read_file` read-then-truncate; `delete_worktree` `git_deleted=True` default; pipeline `approve_step`/`reject_step` token lifecycle; workflow `duplicate` tag/source copy). The `%s` placeholder convention is correct and was **not** flagged (CLAUDE.md's `$N` mandate is stale doc drift).

## Findings

### [BLOCKER] Auth middleware runs a 600k-iteration PBKDF2 + sync DB on the event loop for every protected request
- **Where:** `src/gobby/servers/middleware/auth.py:64-91` (`dispatch`) → `src/gobby/servers/routes/auth.py:36-59` (`_get_auth_credentials`, constructs a fresh `SecretStore` per call) → `src/gobby/storage/secrets.py:98-107` (`_derive_fernet_key`, `iterations=600_000`) and `:130-139` (`_get_fernet`, caches only per-instance)
- **Failure mode:** `AuthMiddleware.dispatch` calls `is_auth_enabled` → `_get_auth_credentials` → `SecretStore(...).get(...)` on every non-public request. Because a brand-new `SecretStore` is built each call, the per-instance `_fernet` cache never helps: each request re-reads the salt, re-derives the Fernet key with 600,000 PBKDF2-SHA256 iterations (~hundreds of ms of pure CPU), then does a synchronous DB fetch + decrypt and a second sync DB query in `validate_session_cookie` — all inside the async middleware, on the loop thread.
- **Why it matters:** Precisely when auth is configured (i.e., the daemon is exposed beyond localhost), every UI/API request serializes behind ~0.2–0.5s of event-loop-blocking CPU, freezing hooks, MCP traffic, and websockets daemon-wide. Trivially DoS-able by request spamming.
- **Minimal fix:** Cache the derived Fernet key / `SecretStore` at module or server scope (invalidate on config save); cache resolved auth credentials; run credential lookup and session validation through `server.run_db(...)` / a threadpool in the middleware.
- **Confidence:** high

### [BLOCKER] `/api/admin/stats` time/project filters emit SQLite SQL against the Postgres hub → silent all-zero data
- **Where:** `src/gobby/servers/routes/admin/_stats.py:48-61` (`_build_filters` emits `strftime('%Y-%m-%dT%H:%M:%S','now',?)` and `?` placeholders), consumed at `:93-150`, `:163-218`
- **Failure mode:** The hub is psycopg/PostgreSQL only (`src/gobby/storage/hub/postgres.py` passes SQL straight to `conn.execute`; no `?`→`%s` translation exists, and `strftime()` is not a Postgres function). Any request with `?hours=N`, `?days=N`, or `?project_id=X` makes every task/session/memory query raise; the broad `except Exception … logger.warning` blocks (`_stats.py:151,191,219`) swallow it and the endpoint returns **200 with all-zero stats**. The metrics-events section in the same file correctly uses `%s` (`:242-296`), so the response mixes real and silently-zeroed data. Unfiltered (`days=0`) requests work, masking the bug.
- **Why it matters:** The dashboard/CLI silently show zeros for any time-windowed or project-scoped stats request — data corruption at the API contract level.
- **Minimal fix:** Rewrite `_build_filters` to Postgres: `AND {created_col} >= NOW() - (%s * INTERVAL '1 hour')` (cf. `newer_than_now_expr` in `src/gobby/storage/sql_dialect.py:66-67`) and `%s` for project_id.
- **Confidence:** high — test gap confirms why it shipped: `tests/servers/routes/admin/test_stats.py:93-105` mocks `db.fetchall` and asserts only `status_code == 200`.

### [BLOCKER] `read_file` loads the entire file into memory regardless of `max_size`
- **Where:** `src/gobby/servers/routes/files.py:296-311`
- **Failure mode:** `raw = await loop.run_in_executor(None, target.read_bytes)` reads the *whole* file, and only afterward checks `len(raw) > max_size` and slices. `max_size` bounds the returned slice, not the read. Opening a large tracked file (multi-hundred-MB data file, model checkpoint, DB dump) in the file browser loads it fully into daemon RAM.
- **Why it matters:** Memory-exhaustion / DoS of the daemon from one normal "click a file" action; the explicit `max_size` guard is silently ineffective. `test_read_truncation` passes only because truncation is applied after the full read, so CI believes the read is bounded.
- **Minimal fix:** Read at most `max_size + 1` bytes (`handle.read(max_size + 1)` in the executor), set `truncated = len(raw) > max_size`, then slice. Add `Query(DEFAULT_MAX_SIZE, ge=0, le=<ceiling>)`.
- **Confidence:** high

### [BLOCKER] `delete_worktree` reports `git_deleted: True` when git deletion raises, orphaning the on-disk worktree
- **Where:** `src/gobby/servers/routes/source_control.py:771-803`
- **Failure mode:** `result` is initialized to `None`. If `target.delete_worktree(...)` raises, the `except Exception` only logs and leaves `result is None`; then `git_deleted = result.success if result is not None else True` evaluates to **`True`**, and the DB tracking row is deleted unconditionally afterward. When git removal throws (locked worktree, permission error), the API returns success and drops the tracking record while the worktree directory remains on disk — now orphaned and untracked.
- **Why it matters:** Silent failure plus state corruption: the UI shows the worktree gone, but the directory and branch linkage persist and Gobby has lost the record needed to clean them up.
- **Minimal fix:** Track the exception explicitly (set a `git_error` flag in the `except`); set `git_deleted = False` when deletion raised; surface it in the response and do not delete the DB record (or mark it) when git deletion genuinely failed vs. simply "no git_manager."
- **Confidence:** high

### [BLOCKER] Pipeline approval tokens are never invalidated or state-guarded — replay corrupts terminal state and can double-run steps
- **Where:** `src/gobby/servers/routes/pipelines.py:387-475` (approve/reject routes; token re-exposed via GET at `:373`); root cause `src/gobby/workflows/pipeline/gatekeeper.py:145-179` (`approve_step`), `:181-223` (`reject_step`), and `src/gobby/storage/pipelines.py:592-675` (`update_step_execution` only ever *sets* `approval_token`; nothing clears it) + `:105-156` (`update_execution_status` has no terminal-state guard)
- **Failure mode:** Neither `approve_step` nor `reject_step` checks that the step is currently `WAITING_APPROVAL`, and the token is never cleared after use. Verified consequences: (1) **reject-after-approve** flips a COMPLETED execution to CANCELLED and its completed step to FAILED; (2) **approve-after-reject** rewrites a rejected step to COMPLETED while the execution stays CANCELLED; (3) **concurrent double-approve** — `PipelineExecutor.execute` has no per-execution mutex, so two simultaneous approves each pass the token lookup and re-run post-gate steps (agent spawns, MCP calls). `GET /api/pipelines/{execution_id}` returns `approval_token` for every step, so the only credential the approve/reject endpoints check is readable long after the gate resolved.
- **Why it matters:** Approval gates are the pipeline system's only human-control point; replay corrupts execution history and re-executes side-effectful steps.
- **Minimal fix:** Require `step.status == WAITING_APPROVAL` in both methods (→ 409 otherwise); NULL the `approval_token` in the same UPDATE that transitions the step; add a terminal-state guard to `update_execution_status` (no CANCELLED-over-COMPLETED); stop returning `approval_token` from the GET.
- **Confidence:** high

### [BLOCKER] Duplicating a bundled workflow/rule/pipeline creates a row that startup sync silently soft-deletes
- **Where:** `src/gobby/servers/routes/workflows.py:183-194` (`POST /{id}/duplicate`) → `src/gobby/storage/workflow_definitions.py:425-444` (`duplicate` copies `tags=original.tags` and hard-codes `source="installed"`); orphan cleanup at `src/gobby/workflows/sync_rules.py:190-202` and `src/gobby/workflows/sync_pipelines.py:265-279`
- **Failure mode:** Bundled definitions carry `tags=["gobby"]`. `duplicate()` copies those tags onto the new row under the new name and stamps `source="installed"`. On every startup, `sync_bundled_rules`/`sync_bundled_pipelines` soft-delete any gobby-tagged row whose **name is not on disk** in bundled YAML. The user's duplicate is not on disk, so it is soft-deleted at the next restart. Same vector via `POST /api/rules` if the body contains `"tags": ["gobby"]` (`src/gobby/mcp_proxy/tools/workflows/_rules.py` uses `definition.get("tags")` verbatim).
- **Why it matters:** Silent user-data loss under a normal UI flow (duplicate a template → restart → row gone); violates the templates-vs-DB ownership contract by treating a user row as a bundled orphan.
- **Minimal fix:** In `duplicate()` (and user-facing `create_rule`), strip the `gobby` tag and set `source="custom"`; alternatively scope orphan cleanup to rows that are gobby-tagged AND `source='installed'`.
- **Confidence:** high

### [IMPORTANT] Sync DB / subprocess on the event loop is the default across the entire route layer
- **Where:** representative sites — `tasks.py:166-183,307-359`; `tasks_lifecycle_routes.py:128-132,207-222`; `tasks_stage_routes.py:158-237`; `tasks_comment_routes.py:50-95`; `projects.py:70-101`; `cron.py:81-251`; `build.py:358,375`; `stages.py:87-153`; `mcp/endpoints/execution.py:140-208` (`_set_context_for_request` called sync at `:304,472,601,701`); `sessions/core.py:376,426-434,583,617`; `sessions/lifecycle.py:25-67,190` → `sessions/core.py:102-108` (`subprocess.run(["git","rev-list",...], timeout=5)` reached from `async def sessions_get`); `chat.py:31-54`; `source_control.py:90-110,181-185`; `files.py:209-435`; `pipelines.py:47-455`; `workflows.py:107-415`; `rules.py:106-378`; `metrics.py:29-40`; `admin/_usage.py:26-27`; `admin/_stats.py:94-296`; all five remaining `configuration_*` modules; `auth.py:90-167`
- **Failure mode:** These `async def` handlers call synchronous psycopg storage methods (and in `sessions/core.py` a blocking `subprocess.run` git call up to 5s) directly on the single event loop. The correct primitive — `server.run_db(...)` (`src/gobby/app_context.py:126`) / `asyncio.to_thread` / `run_in_threadpool` — exists and is used correctly in exactly some places (`tasks_dependency_routes.py:65,135`, `github_triage.py:37`, `build.py` GETs, `agents.py:118`, `chat_attachments.py:287`, `code_index.py`, `admin/_health.py`, `admin/_savings.py`, `admin/_token_timeseries.py`, `workflows.py:143`, `configuration_tool_approvals.py:34-58`, `configuration_import_export.py:341`) — so the inconsistency is within the same files. A slow query or a git call on a large repo stalls the whole daemon.
- **Why it matters:** Repo async contract violated at scale; one heavy HTTP request freezes WebSocket broadcasts, MCP proxy traffic, hook endpoints, statusline ingestion. The `subprocess.run` git path on `GET /api/sessions/{id}` is the sharpest single instance (normal UI panel open can freeze the loop for up to 5s).
- **Minimal fix:** Route all storage calls through `await server.run_db(...)` (or convert pure read handlers to plain `def` so FastAPI offloads them); bridge the git call via `asyncio.to_thread` / `asyncio.create_subprocess_exec`. This is a sweep, not per-endpoint patches.
- **Confidence:** high

### [IMPORTANT] `/api/admin/status` pipeline stats are always zero — manager built with `project_id=""`
- **Where:** `src/gobby/servers/routes/admin/_health.py:374` (`LocalPipelineExecutionManager(db=..., project_id="")`); filter `src/gobby/storage/pipelines.py:900-910`
- **Failure mode:** `count_by_status` filters `WHERE project_id = %s` with `""`, which matches no real executions, so `pipeline_stats` in the status payload is permanently all-zero. The query succeeds, so no warning fires.
- **Why it matters:** The status dashboard silently lies about pipeline activity (`waiting_approval` never surfaces). Same `project_id or ""` scope bug makes `GET /api/pipelines/executions` and `/executions/search` return empty when `project_id` is omitted (`pipelines.py:111,210` → `storage/pipelines.py:158-191`); both web clients omit it when no project is selected.
- **Minimal fix:** Add an unscoped count/list path (treat empty/`None` project_id as "all projects") and use it for the global status and the unscoped executions list.
- **Confidence:** high

### [IMPORTANT] `/api/mcp/*` is fully exempt from auth — unauthenticated arbitrary tool execution and server registration
- **Where:** `src/gobby/servers/middleware/auth.py:32-50` (`_PUBLIC_PREFIXES` includes `/api/mcp/`); mounted with no router-level dependency at `src/gobby/servers/app_factory.py:625`
- **Failure mode:** With UI auth enabled, every `/api/mcp/*` route still bypasses it: `POST /api/mcp/tools/call` (run any tool on any server), `POST /api/mcp/servers` (`add_mcp_server`, accepting unvalidated `transport`/`command`/`args`/`env` — a `stdio` server whose `command` is attacker-chosen is arbitrary code execution on connect), `POST /api/mcp/servers/import`, `DELETE /api/mcp/servers/{name}`.
- **Why it matters:** Defense-in-depth hole at the API boundary. Mitigated by the default `localhost` bind (`src/gobby/config/bootstrap.py:27`), but `bind_host` is operator-configurable; any non-loopback bind exposes unauthenticated RCE-capable endpoints. The exemption is intentional (agents call without cookies) but pairing "no auth" with "register a stdio command + execute arbitrary tools" is the danger.
- **Minimal fix:** Gate state-changing MCP routes behind a local-token / loopback check even while discovery reads stay open; validate `transport`/`command` against an allowlist in `add_mcp_server`.
- **Confidence:** high (routing/exemption verified; severity contingent on non-localhost bind)

### [IMPORTANT] `/api/sessions/` is fully exempt from auth, exposing transcripts and destructive ops
- **Where:** `src/gobby/servers/middleware/auth.py:37` (`/api/sessions/` in `_PUBLIC_PREFIXES`); affected routes include `sessions/messages.py:163` (`GET /{id}/transcript` — full conversation download), `sessions/changes.py:45,66` (working-tree file/diff reads), `sessions/lifecycle.py` `expire`/`bulk-move`/`rename`, `sessions/analytics.py` `stop`/`generate-summary`
- **Failure mode:** When auth is enabled the middleware short-circuits the whole `/api/sessions/` subtree before the cookie check. The exemption exists so hooks/statusline/CLI can register without a cookie, but the coarse prefix also covers transcript download, diff reads, and destructive lifecycle actions.
- **Why it matters:** An unauthenticated client on the bound interface can download full transcripts and source diffs and expire/rename/move sessions, defeating the auth the operator turned on.
- **Minimal fix:** Narrow the exemption to the specific ingestion endpoints (register/find_current/statusline/update_status); require auth for transcript, changes, expire, bulk-move, rename, stop.
- **Confidence:** med-high (prefix match verified; impact assumes auth enabled + reachable bind)

### [IMPORTANT] `/api/admin/status`, `/metrics`, `/config` exempt from auth but expose detailed internals
- **Where:** exemption `src/gobby/servers/middleware/auth.py:41-44`; payload `src/gobby/servers/routes/admin/_health.py:517-549`
- **Failure mode:** With auth enabled, unauthenticated callers can read project id, process memory/CPU/thread/fd counts, DB backend + connection counts + db path-derived size, all configured MCP server names/transports/health, task/session/memory/pipeline counts, Postgres install mode, last shutdown source, and full Prometheus metrics.
- **Why it matters:** Rich recon surface for an unauthenticated network peer.
- **Minimal fix:** Keep `/api/admin/health` public for probes; require the session cookie for `/status`, `/metrics`, `/config`, or gate the exemption on a loopback origin.
- **Confidence:** med (behavior verified; severity depends on whether "read-only admin stays public" is an accepted product decision)

### [IMPORTANT] No application-level timeout in `call_mcp_tool` / `mcp_proxy` — a hung external server hangs the request forever
- **Where:** `src/gobby/servers/routes/mcp/endpoints/execution.py:609-635` (`call_mcp_tool`), `:705-738` (`mcp_proxy`); chain bottoms out at `src/gobby/mcp_proxy/client_manager/invocation.py:28` (`if timeout: wait_for else: await session.call_tool`)
- **Failure mode:** Both handlers call `tool_proxy.call_tool` / `mcp_manager.call_tool` with no timeout; with `timeout=None` the downstream `session.call_tool` is awaited unbounded. A slow/hung/malicious external MCP server pins the HTTP request and its connection indefinitely. Inconsistent with the discovery path (`discovery.py:159-167`), which correctly budgets `_mcp_call_timeout(server)`.
- **Why it matters:** Resource exhaustion under normal misbehavior of any configured external server — on the path that runs arbitrary tools.
- **Minimal fix:** Pass `_mcp_call_timeout(server)` through to the call, or wrap the awaits in `asyncio.wait_for` and return a `success=False` timeout envelope.
- **Confidence:** high

### [IMPORTANT] Login uses non-constant-time `!=` comparison; password stored recoverably, not hashed; no rate limiting
- **Where:** `src/gobby/servers/routes/auth.py:101` (`if req.username != username or req.password != stored_password:`); storage `auth.py:50-54` + `src/gobby/storage/secrets.py` (Fernet, reversible)
- **Failure mode:** Plain `!=` is not constant-time; the password is stored Fernet-encrypted (recoverable with the salt file + machine-id, both on the same host) rather than as a one-way hash; no failed-attempt delay/lockout on `/api/auth/login`. (The accidental PBKDF2 throttling per attempt disappears once the auth-middleware Blocker is fixed by caching.)
- **Why it matters:** This is the single auth gate for network-exposed deployments; reversible storage means DB+salt compromise yields the cleartext password (likely reused elsewhere).
- **Minimal fix:** `secrets.compare_digest` for both fields; store a salted argon2/bcrypt hash; add a failed-attempt counter/delay.
- **Confidence:** high for the facts; med on practical timing-channel exploitability

### [IMPORTANT] DELETE /api/tasks/{id}: intended 404 rewritten to 500, "has children" refusal returns 404
- **Where:** `src/gobby/servers/routes/tasks.py:466-485`
- **Failure mode:** (1) `raise HTTPException(404, "Task not found")` at `:478` is inside the `try` with no `except HTTPException: raise` guard (unlike `update_task` at `:456-457`), so it falls into `except Exception` and becomes `HTTPException(500, detail="404: Task not found")`. (2) `delete_task` raises `ValueError` for has-children-and-`cascade=False` (`src/gobby/storage/tasks/_manager.py:724-725`); `:481` maps `ValueError`→404, telling the client a live task tree doesn't exist.
- **Why it matters:** Wrong status codes on a destructive endpoint; the 404-for-refusal misleads clients into believing a delete succeeded.
- **Minimal fix:** Add `except HTTPException: raise` before the broad handler; map the has-children `ValueError` to 409.
- **Confidence:** high

### [IMPORTANT] GET /api/tasks `total` ignores every filter except project; unbounded/negative limit/offset
- **Where:** `src/gobby/servers/routes/tasks.py:297-298` (no `ge`/`le`), `:336,343-346`
- **Failure mode:** With no stage filter, `total = count_tasks(project_id=...)` but `count_tasks` only supports `project_id`/`current_stage_state` (`storage/tasks/_manager.py:831-835`), so with `closed`/`priority`/`label`/`search`/`task_type`/`parent_task_id`/`claimed` filters active, `total` is the whole-project count while `tasks` is the filtered page (the stage path uses the opposite `total = len(filtered)`). Separately, `limit`/`offset` have no bounds: negative `limit` → Postgres error → unhandled 500; negative `offset` feeds Python slicing at `:344` and returns a wrong page with 200; the stage path silently truncates at a hardcoded `limit=10000` fetch (`:336`).
- **Why it matters:** Paginating clients render phantom pages / wrong counts; trivial hostile input crashes the route; tasks beyond 10k silently vanish.
- **Minimal fix:** Add the filter params to `count_tasks` and pass them; make both paths agree; `limit: Query(50, ge=1, le=…)`, `offset: Query(0, ge=0)`; page the stage query in SQL.
- **Confidence:** high

### [IMPORTANT] GET /api/tasks: `stage_state` filter is a silent no-op without `stage`
- **Where:** `src/gobby/servers/routes/tasks.py:285-352` (consumed only inside the `stage_filters` loop at `:313-321`)
- **Failure mode:** `?stage_state=in_progress` with no `stage` returns the unfiltered list (stages attached), not tasks at that state — no error.
- **Minimal fix:** Reject `stage_state` without `stage` (400), or implement state-only filtering across all stages.
- **Confidence:** high

### [IMPORTANT] POST /api/tasks with a nonexistent project_id → 500 with raw DB error
- **Where:** `src/gobby/servers/routes/tasks.py:124-128` (`_resolve_project` returns the client ID unvalidated), `:372-397`
- **Failure mode:** `tasks.project_id` is `NOT NULL REFERENCES projects(id)`; the create path doesn't validate project existence or catch FK violations, so a bogus `project_id` raises `ForeignKeyViolation` (not a `ValueError`) → `except Exception` → `HTTPException(500, detail=str(e))`, echoing the raw Postgres constraint/table names.
- **Minimal fix:** Validate the resolved project exists in `_resolve_project` (404/400), or catch `psycopg.errors.ForeignKeyViolation` → 400.
- **Confidence:** med (schema FK + missing validation + catch structure verified; not executed end-to-end)

### [IMPORTANT] POST /api/tasks/{id}/comments: invalid `parent_comment_id` → 500; cross-task threading allowed
- **Where:** `src/gobby/servers/routes/tasks_comment_routes.py:72-103` (insert at `:80-91`)
- **Failure mode:** `parent_comment_id` is inserted as-is against an FK to `task_comments(id)`; a nonexistent parent raises `ForeignKeyViolation` (uncaught → 500), and a parent belonging to a *different task* passes the FK and is persisted, corrupting thread integrity.
- **Minimal fix:** When set, verify the parent exists and its `task_id` matches the resolved task; 400 otherwise.
- **Confidence:** high

### [IMPORTANT] DELETE comment ignores rowcount — deleting a nonexistent comment reports success
- **Where:** `src/gobby/servers/routes/tasks_comment_routes.py:105-114`
- **Failure mode:** `db.execute(...)` returns a cursor whose `rowcount` is discarded; the route returns `{"deleted": True}` for any `comment_id` (nonexistent or belonging to another task). Contrast `remove_dependency` (`tasks_dependency_routes.py:182-183`) and `delete_task`, which 404 on no-op.
- **Minimal fix:** `if cursor.rowcount == 0: raise HTTPException(404, "Comment not found")`.
- **Confidence:** high

### [IMPORTANT] Cron jobs can be created/updated into a dead state; system-row rejection returns 500
- **Where:** `src/gobby/servers/routes/cron.py:93-115` (create), `:36-48,132-180` (update/delete)
- **Failure mode:** `create_job` accepts `schedule_type="cron"` with no `cron_expr` (or `"once"` with no `run_at`); storage persists it and `compute_next_run` returns `None` (`storage/cron.py:108-129`) — created "successfully," never fires. `UpdateCronJobRequest` types `schedule_type`/`action_type` as plain `str` (the create model uses `Literal`), so PATCH can persist values the scheduler can't execute. Separately, `update_job`/`delete_job` raise `SystemRowProtected` (a `ValueError` subclass) for gobby-managed rows; routes catch only `HTTPException` then `Exception`, so every system-row rejection becomes a 500 with the internal message.
- **Minimal fix:** Validate schedule-type/field pairing and reuse the create model's `Literal` types on update; add `except ValueError → 403/400` (or catch `SystemRowProtected`) in update/delete/toggle.
- **Confidence:** high

### [IMPORTANT] PUT /api/projects/{id} applies partial updates then returns 400; no transaction boundary
- **Where:** `src/gobby/servers/routes/projects.py:162-206`
- **Failure mode:** The handler commits `pm.update(...)` and saves/migrates approval rules *before* validating `validation_detection`, whose `ValidationError` raises 400 at `:191` — so a rejected request has already committed the rename/repo-path change and rule writes. The DB write + two file saves + two clears span lines 163-206 with no transactional boundary.
- **Why it matters:** Violates related-fields-atomic; a failed request mutates state; retries compound drift.
- **Minimal fix:** Validate all payload-derived work before the DB update; validate-first, mutate-second.
- **Confidence:** high for the ordering; med for the file-migration interleavings

### [IMPORTANT] PUT /api/projects/{id}/github-triage: explicit `"repositories": null` crashes with 500
- **Where:** `src/gobby/servers/routes/projects.py:236-253` (`:249` `tuple(values.get("repositories", current.repositories))`)
- **Failure mode:** `repositories` is `list[str] | None`; with `model_dump(exclude_unset=True)`, a body `{"repositories": null}` survives and `tuple(None)` raises `TypeError` → 500. Same pattern puts explicit `null` for bool/int fields straight into the `GitHubTriageConfig` dataclass (no validation), persisting `None` where the reconcile loop expects an int.
- **Minimal fix:** Drop `None`-valued keys after `model_dump(exclude_unset=True)`, or use non-optional field types.
- **Confidence:** high

### [IMPORTANT] GitHub webhook discloses project/config state before signature verification
- **Where:** `src/gobby/servers/routes/github_triage.py:28-46`; ordering in `src/gobby/github_triage/service.py:141-149`
- **Failure mode:** `accept_webhook_delivery` checks project existence (`TriageWebhookError`→401 "Unknown project") and triage enablement (`TriageDisabledError`→403) *before* `_validate_signature`. An unauthenticated caller distinguishes project-missing vs exists-disabled vs exists-enabled, enumerating project IDs and triage config without the secret. This is the one deliberately public endpoint (auth-exempt, HMAC-gated).
- **Minimal fix:** Verify HMAC before reporting project/config distinctions; collapse pre-verification failures to a uniform 401.
- **Confidence:** high (ordering verified; severity tempered by UUID project IDs and usual LAN/localhost bind)

### [IMPORTANT] `bulk_move_sessions` swallows per-row errors inside one transaction → misleading all-or-nothing
- **Where:** `src/gobby/servers/routes/sessions/lifecycle.py:111-139`
- **Failure mode:** Per-id `db.execute("UPDATE sessions …")` runs inside a single ambient transaction; a real DB error on one row (e.g. unvalidated `target_project_id` FK) aborts the psycopg transaction, but `except Exception` appends to `errors` and continues — every later statement then raises `InFailedSqlTransaction`, and the block can't commit. `moved_ids` was populated as if rows moved; the request usually surfaces as 500 and the intended partial-success semantics never apply.
- **Minimal fix:** Validate `target_project_id` up front; fail the whole batch cleanly, or wrap each row in its own savepoint/transaction.
- **Confidence:** med (psycopg abort-on-error is well established; confirming needs a triggered row failure)

### [IMPORTANT] Agent definition read-modify-write without a transaction — lost updates
- **Where:** `src/gobby/servers/routes/agents.py:353-431` (`update_definition`), `:486-586` (`patch_rules`/`patch_rule_selectors`/`patch_variables`)
- **Failure mode:** Each handler does `get` → `json.loads(definition_json)` → mutate → `update(...)` as two separate DB ops with no transaction or version check. Concurrent edits clobber each other's changes on the blob.
- **Minimal fix:** Do the read-modify-write inside `db.transaction_immediate()` (row lock) or add a version/`updated_at` CAS.
- **Confidence:** med (race real; likelihood bounded by single-operator UI)

### [IMPORTANT] `POST /api/workflows` accepts unvalidated `definition_json`/`workflow_type`, breaking rules endpoints; duplicate names → 500
- **Where:** `src/gobby/servers/routes/workflows.py:197-253`; consumer break at `src/gobby/servers/routes/rules.py:113` (`json.loads(row.definition_json)` in `list_groups`)
- **Failure mode:** `definition_json` is stored verbatim. A caller can create `workflow_type="rule"` rows with invalid JSON / a body `RuleDefinitionBody` would reject, bypassing the validation `POST /api/rules` enforces; one such row then 500s `GET /api/rules/groups`, `/tags`, and `GET /api/rules` during `json.loads`. Also, the unique index on `(name, COALESCE(project_id,'__global__'), source)` makes a duplicate name raise `UniqueViolation` → bare 500 (vs the rules endpoint's correct 409).
- **Minimal fix:** Parse `definition_json` and validate with `RuleDefinitionBody` when `workflow_type=="rule"`; map unique-violation → 409; make rules listings skip-and-log unparseable rows.
- **Confidence:** high

### [IMPORTANT] Unbounded `await file.read()` on vision, voice, and wiki uploads
- **Where:** `src/gobby/servers/routes/llm.py:134` (`extract_vision`), `src/gobby/servers/routes/voice.py:234` (`transcribe_audio`), `src/gobby/servers/routes/wiki.py:123-135,404-420` (`/attach` → `_stage_upload`, no byte cap / disk check)
- **Failure mode:** Full upload buffered into memory (llm/voice) or streamed to a temp file with no size limit (wiki) before processing, with no content-type check. The codebase's own `chat_attachments.py:316-332` enforces a configurable `max_file_bytes` ceiling + `_ensure_disk_space`. A large upload OOMs (llm/voice) or fills the disk (wiki).
- **Minimal fix:** Enforce a max-size chunked read (413 past the cap, unlink temp), mirroring `chat_attachments`.
- **Confidence:** high on code; med on exposure (default localhost bind)

### [IMPORTANT] Wiki `/ingest` ingests arbitrary absolute filesystem paths with no project scoping
- **Where:** `src/gobby/servers/routes/wiki.py:137-158,343-348` (`ingest` → `_ingest_paths` → `gateway.ingest_file`)
- **Failure mode:** `paths`/`path` come from the body, validated only as non-empty strings, and passed verbatim to the gwiki binary as `["ingest-file", str(path)]`. Absolute paths like `/etc/passwd` or any path outside the project are accepted; content is then readable back through the wiki read/search endpoints. Unlike `skills.py` (`_resolve_project_import_path`) and `files.py` (`_resolve_safe_path`), there is no containment check.
- **Minimal fix:** Resolve and confine ingest paths to the project root (reuse the `skills.py` pattern), rejecting absolute/`..`-escaping paths with 403.
- **Confidence:** med (depends on whether the gwiki binary imposes its own root confinement — confirm gwiki path handling)

### [IMPORTANT] `list_branches` caches a degraded result built under a swallowed exception
- **Where:** `src/gobby/servers/routes/source_control.py:302-307`
- **Failure mode:** `except (subprocess.TimeoutExpired, Exception) as e:` (the `Exception` arm subsumes `TimeoutExpired`) logs a warning then caches and returns whatever partial `branches` list was built — for `_GIT_TTL` (10s). A transient/programming error yields a silently-truncated branch list cached for the TTL.
- **Minimal fix:** Narrow the catch to expected git/OS exceptions; only `_set_cached` on a clean pass.
- **Confidence:** med

### [IMPORTANT] `statusline_update` passes unvalidated raw body to `update_usage`
- **Where:** `src/gobby/servers/routes/sessions/core.py:426-434`
- **Failure mode:** Untyped `body = await request.json()`; `body.get("input_tokens", 0)` etc. flow straight into `sm.update_usage(...)` uncoerced, while the WebSocket broadcast a few lines later defensively wraps the same values in `int(... or 0)` (`:457-462`). A malformed POST corrupts usage accounting or raises inside `update_usage` (→ 500 for what should be 400).
- **Minimal fix:** A Pydantic request model (or `int(... or 0)` coercion as the broadcast path does).
- **Confidence:** med

### [IMPORTANT] `POST /api/config/template` silently wipes unrelated config_store namespaces
- **Where:** `src/gobby/servers/routes/configuration_templates.py:83` (`delete_all_except(config_store, masked_secret_keys)`), helper `configuration_secrets.py:71-80`
- **Failure mode:** Saving the YAML template deletes every `config_store` row not in `masked_secret_keys`, then re-inserts only the YAML-vs-defaults diff. `config_store` also holds `ui_settings.*` and `tool_approvals.global_rules`, which are not part of `DaemonConfig` (`extra: "ignore"`). A template PUT whose YAML omits those keys destroys persisted UI settings and resets global tool-approval rules to defaults with no warning.
- **Minimal fix:** In `delete_all_except`, preserve non-`DaemonConfig` namespaces (`ui_settings.`, `tool_approvals.`), or restrict the flow to keys present in the `DaemonConfig` schema.
- **Confidence:** med-high (key names, `extra: "ignore"`, delete SQL verified; not executed)

### [IMPORTANT] `POST /api/config/import` is not atomic across config and prompts
- **Where:** `src/gobby/servers/routes/configuration_import_export.py:278-293` (config replaced+committed inside `persist_imported_config`) then `:357-423` (prompt writes afterward, outside that transaction)
- **Failure mode:** If any prompt write fails, the handler returns 400/500 but the config store was already wiped (`delete_all()` at `:279`) and replaced in its own committed transaction; prompts are partially written. A failed import leaves a half-imported state contradicting the error.
- **Minimal fix:** Wrap config + prompts in one ambient `config_store.db.transaction()`.
- **Confidence:** high

### [IMPORTANT] `PUT /api/config/values`: second validation failure after commit → 400 with changes persisted + runtime drift
- **Where:** `src/gobby/servers/routes/configuration_values.py:156-184`
- **Failure mode:** Pre-persist validation uses `$secret:<name>` reference strings; post-persist validation (`:181`) uses raw plaintext secrets. A field whose validator treats those differently passes the first and fails the second — after the transaction committed; the client gets 400, the values are saved, and `set_runtime_config` is skipped, so running config diverges from the DB until restart.
- **Minimal fix:** Build and validate the final runtime config (with the value shape that will be applied) before opening the transaction.
- **Confidence:** med

### [IMPORTANT] `/api/admin/restart`: blocking `launchctl` subprocess on the loop; error path still restarts; lock can wedge
- **Where:** `src/gobby/servers/routes/admin/_lifecycle.py:252,260-275,286` → `src/gobby/cli/installers/service.py:267-273,505-516`
- **Failure mode:** (a) `_should_restart_via_service_manager()` runs `subprocess.run(timeout=10)` synchronously in the async handler, freezing the daemon. (b) `_spawn_restart_helper` is launched (`:260`) *before* `write_shutdown_source`/`_request_runner_shutdown` (`:267-270`); if either throws, the route returns `{"status":"error"}` while the detached helper already proceeds to SIGTERM/SIGKILL. (c) On success the restart lock is never released; if the detached helper fails, subsequent `/restart` calls return `already_restarting` forever.
- **Minimal fix:** `await asyncio.to_thread(_should_restart_via_service_manager)`; spawn the helper only after shutdown is confirmed; release the lock if shutdown wasn't initiated.
- **Confidence:** high for (a)(b); med for (c)

### [IMPORTANT] `/api/admin/savings/record` takes a raw `dict[str, Any]` body with no validation
- **Where:** `src/gobby/servers/routes/admin/_savings.py:64-112`
- **Failure mode:** Raw `body.get(...)` (unlike every sibling Pydantic route); non-integer/negative token counts or a non-dict `metadata` flow into `tracker.record*` and surface as an unhandled 500 (should be 422), and pollute the ledger with negative rows.
- **Minimal fix:** A `RecordSavingsRequest(BaseModel)` with typed, `ge=0` fields.
- **Confidence:** high

### [IMPORTANT] Inconsistent error envelopes leak raw exception text on 500s
- **Where:** clusters in `sessions/*` and `agent_spawn.py` (`detail=str(e)` — e.g. `agent_spawn.py:428,481,505,567`, `sessions/core.py:660`, all of `sessions/lifecycle.py`), `mcp/endpoints/*` (`{"success": False, "error": str(e)}` ubiquitous), `pipelines.py:321-323`, `tasks.py:395-397,462-464,483-485`, `cron.py` (many) — vs generic "Internal server error" in `agents.py`/`analytics.py`/`communications.py`/`workflows.py`/`rules.py`, vs middleware `{"error": ...}`
- **Failure mode:** Raw `str(e)` returns DB error detail, SQL fragments, file paths, and internal type names to clients (worse on the unauthenticated `/api/mcp/*` and `/api/sessions/*` paths); and clients see three different error-envelope shapes across the API.
- **Minimal fix:** Standardize 500s to a generic detail + `logger.error(..., exc_info=True)`; reserve specific `detail` for 4xx.
- **Confidence:** high

### [IMPORTANT] `_health.py` swallows exceptions with bare `except Exception: pass` in four places
- **Where:** `src/gobby/servers/routes/admin/_health.py:440-441,459-460,473-474,502-503`
- **Failure mode:** Errors in fd-usage, last-shutdown, agent-stats, and db-size sections disappear with no log line; a recurring agent-stats failure permanently zeros `agents.running` with no trace.
- **Minimal fix:** `logger.debug(..., exc_info=True)` in each, matching the file's pattern elsewhere.
- **Confidence:** high

### [IMPORTANT] `POST /api/voice/transcribe` returns HTTP 200 for every failure class
- **Where:** `src/gobby/servers/routes/voice.py:215-287`
- **Failure mode:** Capability-unavailable, validation errors, timeouts, and unexpected exceptions all return 200 with `{"error": ..., "text": ""}`; sibling `llm.py`/`embeddings.py` return 400/500. Tests lock the 200 contract in, so it's deliberate drift, but status-code-driven clients treat failures as success.
- **Minimal fix:** Return `JSONResponse(status_code=...)` for the error arms (keep the payload), update tests; at minimum document the contract.
- **Confidence:** high

### [IMPORTANT] `restore_from_template` / drift cache is name-keyed across all definition types
- **Where:** `src/gobby/servers/routes/workflows.py:272-301`; `src/gobby/workflows/template_hashes.py:197-218` (single name-keyed cache across rules/pipelines/variables/agents/skills)
- **Failure mode:** `has_drift(row)`/`get_template_json(row.name)` ignore `workflow_type`. A user definition sharing a name with a bundled template of a *different* type is reported as drifted and, on restore, has its body overwritten with the other type's template.
- **Minimal fix:** Key the caches by `(workflow_type, name)` and pass `row.workflow_type` through.
- **Confidence:** med-high (collision precedent in repo memory; no live collision enumerated)

### [IMPORTANT] Auto-generated spawn prompt computed then discarded in `web_chat` mode
- **Where:** `src/gobby/servers/routes/agent_spawn.py:206-225` (prompt built), `:281-285` (response omits it)
- **Failure mode:** In the `web_chat` branch `_do_spawn` builds `prompt` (incl. the auto-generated task prompt when `req.prompt` is empty) but `AgentSpawnResponse` returns only `conversation_id`; a stale comment says "Store the prompt so the frontend can send it." A web_chat agent on a task with no explicit prompt gets no task context.
- **Minimal fix:** Add the prompt to `AgentSpawnResponse` and populate it, or drop the dead build + comment.
- **Confidence:** med (depends on intended frontend flow)

## Nits

### [NIT] Broad `except Exception` → 500 leaks internals across files
`tasks.py:395-397,462-464,483-485`; `cron.py:89-257` (many); `_testing.py:102,184,230,331`. Log `exc_info=True`, return a generic detail. Confidence: high.

### [NIT] Build control response/validation inconsistency
`build.py:369-393` (resume wraps `{success,result,error}`, errors as 400 JSON) vs `:352-367,395-439` (stop/clean/restart bare dicts + plain HTTPException); `BuildControlRequest` (`:72-95`) lacks `extra="forbid"` while `BuildRequest` (`:44`) has it; `_restart_option_field_was_supplied` (`:122-128,218-222`) treats explicitly-supplied default values as "not supplied." Confidence: high / med.

### [NIT] Stage transitions discard session attribution; force-move stores a raw unresolved header
`tasks_stage_routes.py:157-237` — `by_session_id=None` hardcoded for start/submit/approve/reject/complete/fail/add/remove; `x-gobby-session-id` passed unresolved only on force move (`:209`) while lifecycle routes resolve refs. Confidence: high (behavior) / med (need).

### [NIT] `close_task` treats empty-string `commit_sha` as "no commit"
`tasks_lifecycle_routes.py:206` — `if body.commit_sha:` degrades `""` to a no-commit close. Use `is not None` + a non-empty validator. Confidence: high.

### [NIT] Dead routeless `router` / compatibility shim
`stages.py:78` (`router = APIRouter(...)` never gets routes), `stage_routes.py:1-5` re-exports it; only `create_stages_router` is mounted (`app_factory.py:622`). Confidence: high.

### [NIT] Private-attribute reach-through from routes
`tasks.py:181` (`stage_states._state_from_row`), `dependencies.py:67,73,85` (`server._internal_manager`/`_tools_handler`/`_mcp_db_manager`). Expose public accessors. Confidence: high.

### [NIT] N+1 owner-session lookups during list serialization
`tasks.py:202-249` (`_owner_session_ref` → `session_manager.get` per task); `projects.py:70-83` (`_get_project_stats`, 3 queries/project). Batch with `WHERE id IN (...)`. Confidence: high.

### [NIT] Cron jobs default to `project_id=""`
`cron.py:25,98-99` — omitting `project_id` persists `""` instead of resolving the current project; such jobs are invisible to project-scoped `list_jobs`. Confidence: med.

### [NIT] Path components built from unsanitized `external_id`
`sessions/messages.py:113,172,229` interpolate `external_id` (from the public register endpoint) into archive paths with no charset validation. Validate against a safe charset or assert `relative_to(archive_dir)`. Confidence: low (normal external_ids are UUIDs).

### [NIT] Fire-and-forget `ensure_future` without a stored reference
`agent_spawn.py:395-404` — task may be GC'd before completion; use `server.register_background_task`. `memory.py:417-418,501-503` pokes `server._background_tasks` directly instead of `register_background_task` (and the rebuild task lacks a `name=`). Confidence: high / med.

### [NIT] Redundant work and no-op statements in sessions core
`sessions/core.py:382` (`prune_trackers(now)` duplicates the prune in `record_statusline_seen`), `:372` (`raise … from None` outside any `except`). Confidence: high.

### [NIT] Export Content-Disposition / definition name unsanitized
`agents.py` export handler builds `filename="{name}.yaml"` from a stored name with no charset validation on create; `auth.py:64` names plaintext `password_hash`, `:102` logs the raw username (log-injection), `:113-124` cookie lacks `secure`. Validate names (reuse `_SAFE_DEFINITION_NAME`); use `%r` logging; set `secure=True` behind TLS. Confidence: high / low.

### [NIT] `cancel_agent_run` kill/DB-update not atomic
`agents.py:675-702` — `kill_agent` then `manager.cancel`; if `cancel` raises after a successful kill, the DB shows a zombie "running" row. Update status in try/finally. Confidence: med.

### [NIT] `_get_git_tracked_files` / `git_status` / `chat.py` swallow bare `Exception`
`files.py:144-145,418-419` (broad catch → "no git info"); narrow to `(OSError, subprocess.SubprocessError)`. Confidence: high.

### [NIT] `serve_image` serves SVG inline as `image/svg+xml`
`files.py:313-333` — SVGs can carry scripts; `chat_attachments.py` uses `_content_disposition`. Serve SVG as attachment or add a CSP. Confidence: low.

### [NIT] Unbounded/negative numeric query params
`files.py:256` (`max_size` no bounds — see Blocker), `source_control.py:360,681,808` (`limit`/`hours` unbounded), `memory.py:350` (`memory_limit*10` unclamped). Add `Query(ge=…, le=…)`. Confidence: med.

### [NIT] Dead SQLite branches / dead response models / dead 400-branches
`admin/_token_timeseries.py:42-52` (`strftime` fallback behind hardcoded `is_postgres()->True`); `pipelines.py:28-44` (`PipelineRunResponse`/`PipelineApprovalResponse` unused), `:433-437` (resume `ValueError`→404 "Invalid token" masks internal errors), `:321-323` (`run_pipeline` leaks `str(e)`); `workflows.py:313,329` (unreachable 400-on-"template" branch). Confidence: high.

### [NIT] Non-atomic read-modify-write toggle (workflows)
`workflows.py:222-234` (`toggle_workflow`: `get` then `update(enabled=not …)`) — concurrent toggles net to a no-op; rules toggle takes explicit `enabled` (`rules.py:370-378`). Confidence: high (race) / low (impact).

### [NIT] Lifecycle endpoints return 200 with `{"status":"error"}`
`admin/_lifecycle.py:229-234,288-292,312-347` — shutdown/restart/reload failures indistinguishable from success by status code. Raise HTTPException 500/503. Confidence: high.

### [NIT] `_testing.py` check-then-insert race
`admin/_testing.py:70-88` — project existence check then INSERT, duplicate PK → 500 under concurrency. `ON CONFLICT DO NOTHING`. (Guarded by `test_mode`.) Confidence: high.

### [NIT] `GET /api/config/prompts` `total` is page-local after dedupe
`configuration_prompts.py:80-88` — `"total": len(prompts)` with `limit`/`offset` pagination; `total`==`count` always. Confidence: med.

### [NIT] HTTPException from inside AuthMiddleware surfaces as 500 not 503
`auth.py:51` → `_database.py:20-26`, invoked from `middleware/auth.py:72-74`; Starlette's `ExceptionMiddleware` sits inside user middleware, so a DB-down `HTTPException(503)` becomes a 500. Catch and return a 503 `JSONResponse` directly. Confidence: med.

### [NIT] MCP-route hygiene: raw `dict.get()` bodies, duplicated helper, over-broad webhook catch, hold-open
`mcp/endpoints/discovery.py:343-348,432-436`, `execution.py:580-582`, `server.py:112-142`, `registry.py:29-30,155-158` (raw `body.get` for `top_k`/`min_similarity`/server-config — add Pydantic/`isinstance` checks); `_mcp_manager_is_connected` duplicated with differing impls (`server.py:37-43` instance vs `registry.py:22-28` class); `webhooks.py:88` (`except (ValueError, Exception)` + `from None` — catch `json.JSONDecodeError`); `hooks.py:367,390` (web-chat hold-open pins a connection up to 300s — bounded but consider polling); envelope-shape divergence between the `tool_proxy` path and fallback (`execution.py:616` vs `:639-643`, `:712` vs `:753-757`). Confidence: high (facts) / low (some impacts).

### [NIT] Misc route hygiene: reserved rule names, heavy import on loop, prod `assert`, embeddings metadata
`rules.py:106-141` (`groups`/`tags`/`bulk-toggle` literal segments shadow `/{name}` — reserved-name validation needed); `voice.py:187` (`importlib.import_module("faster_whisper")` blocks the loop seconds on first import — use `find_spec`), `:313` (`assert registry is not None` stripped under `-O`); `embeddings.py:106-133,178-185` and `llm.py:70-73` emit raw `binding.to_dict()` metadata (internal endpoint URLs) bypassing the file's own `_SAFE_BINDING_METADATA_KEYS` whitelist — no secrets found, but route metadata through the whitelist. Confidence: high (code) / med (severity).

## Systemic patterns

1. **Synchronous work on the asyncio event loop is the default, the off-loop bridge is the exception.** Sync psycopg storage calls (and one blocking `subprocess.run` git call, and a 600k-iteration PBKDF2 in the auth middleware) run directly in `async def` handlers across essentially every file in scope. `server.run_db` / `asyncio.to_thread` / `run_in_threadpool` exist and are used correctly in a minority of places (often in the *same file* as a violating handler). This is the single highest-value fix and should be a sweep. Two Blockers (auth PBKDF2, sessions git subprocess) are acute instances; the rest is pervasive latency/HOL-blocking.

2. **Broad `except Exception` that swallows or stringifies failures.** Three sub-shapes: (a) swallow-and-default → silently wrong data (`_stats.py` ships a whole broken SQL dialect because every query is individually caught; `_health.py` has four bare `pass` blocks; `list_branches` caches a degraded result); (b) `except Exception → HTTPException(500, str(e))` leaks SQL/paths/types to clients; (c) bundling typed errors (`TaskNotFoundError`, `ValueError`, `SystemRowProtected`, `ForeignKeyViolation`) into one catch produces wrong status codes (404-for-refusal, 500-for-system-row, 500-for-bad-input).

3. **Validation asymmetry and raw `dict.get()` bodies.** Create models use `Literal`/validators/`extra="forbid"`; their update/control twins use bare `str` / no extra policy, letting PATCH corrupt what POST protects (cron, build). Several POST endpoints (`statusline_update`, `bulk_move_sessions`, `admin/savings/record`, all MCP body handlers, `workflows` `definition_json`) skip Pydantic entirely, yielding 500s where 400s belong and letting invalid data reach storage.

4. **Inconsistent error/response envelopes across the API.** `{"status":"success"}` (cron), bare dicts (tasks/projects), `{"success","result","error"}` (build resume, MCP), generic `"Internal server error"` (agents/workflows/rules), 200-with-`{"error":...}` (voice), middleware `{"error":...}`. Clients need per-route knowledge of both shape and which HTTP code means failure.

5. **Auth boundary is a coarse public-prefix list paired with powerful endpoints.** `/api/mcp/`, `/api/sessions/`, and `/api/admin/status|metrics|config` are blanket-exempt; when auth is enabled (i.e., off-localhost) this exposes arbitrary tool execution + stdio-server registration, transcript/diff download + destructive session ops, and detailed recon — plus the webhook signature-ordering disclosure on the one intentionally-public path. The login itself uses non-constant-time comparison and reversible password storage with no rate limit.

6. **Trusting storage-layer defaults and shared tables at the API boundary.** `project_id or ""` scoping silently returns empty/zero (admin status, pipeline executions); `source="installed"` + verbatim `tags` copying on duplicate/import blurs the bundled-vs-user line the startup sync depends on (the duplicate-then-soft-delete data-loss Blocker); `config_store` is a shared dumping ground whose `delete_all()`/`delete_all_except()` flows collide with unrelated namespaces (UI settings, tool-approval rules).

7. **Route tests mock the storage layer and assert shape, not behavior.** `test_stats.py`, `test_pipelines.py`, `test_build.py`, `test_voice_routes.py`, and the task-list tests mock `db`/managers and check status codes — so the SQLite-dialect Blocker, the `project_id=""` scope bugs, token replay, and blocking-on-loop behavior are all invisible to CI. Several findings here have no covering test precisely because the seam between routes and real storage semantics is never exercised.
