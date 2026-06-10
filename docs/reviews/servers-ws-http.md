# Review: servers (websocket + http core)

- **Scope:** `src/gobby/servers/` **excluding** `routes/` (~20,709 lines): HTTP bootstrap (`http.py`, `app_factory.py`, middleware, `exception_handlers.py`, `uvicorn_shutdown.py`), the WebSocket server + broadcast (`websocket/server.py`, `broadcast.py`), message handlers (`websocket/handlers/`), pending interactions, the managed chat-session stack (`chat_session*.py`, `websocket/chat/` core + backends), tmux/voice bridges, and provider-model catalogs. Split across 7 parallel reviewers by subsystem; synthesized and Blocker-verified against source.
- **Reviewer:** Claude (Fable 5) — 7 general-purpose review agents + synthesizer verification
- **Commit / branch:** `0.5.0` @ 7790ee71f
- **Summary:** 4 Blocker · 28 Important · 21 Nit — the dominant systemic problem is **synchronous DB / FS / crypto work on the WebSocket and middleware event loop** (a stall here freezes every connected client's live stream), and a cluster of **stream-lifecycle gaps** where interrupt/disconnect/error exits skip persistence, status reset, and resource cleanup that only the happy-path "Done" branch performs.

> Verification note: all 4 Blockers were re-read directly against source — the plan-file out-of-repo write bypass (`find_out_of_repo_write_path` `continue`s on any plan-pattern path before the containment check; the regex matches absolute/`..` paths), the Droid generator catching `asyncio.CancelledError` in its `except` tuple (`droid.py:367`), `_stream_chat_response` persisting assistant content and resetting status only via `_handle_done` (cancel/disconnect/error/`finally` skip both), and the attached-session TTS pipeline/offset map having no teardown on detach or disconnect. `%s` placeholders were not flagged (stale CLAUDE.md `$N` drift).

## Findings

### Security & permissions

### [BLOCKER] Out-of-repo write guard is bypassable via the plan-file pattern (absolute / traversal paths)
- **Where:** `src/gobby/servers/tool_approvals.py:486-509` (`find_out_of_repo_write_path` does `if is_plan_file_path(path_value): continue` *before* resolving and containment-checking), `:482-483` (`is_plan_file_path`), pattern at `src/gobby/servers/chat_session_helpers.py:34` (`_PLAN_FILE_PATTERN = ^(?:.*[/\\])?\.(?:claude|gobby|gemini|qwen|codex)[/\\].*\.md$`), plan-mode allow at `src/gobby/servers/chat_session_permissions.py:240-244`
- **Failure mode:** The plan-file regex uses `(?:.*[/\\])?` as its prefix, so it matches **absolute paths and `../` traversal** as long as the path contains a `.claude/`/`.gobby/`/`.gemini/`/`.qwen/`/`.codex/` segment and ends in `.md` (e.g. `/Users/victim/.claude/plans/p.md`, `../../../home/user/.gobby/x.md`). `find_out_of_repo_write_path` `continue`s past those paths, so the out-of-repo guard never resolves or containment-checks them and returns `None` (write allowed). In plan mode, `_resolve_tool_permission` (`:240-244`) auto-allows `Write`/`Edit` to any plan-pattern path and returns before the out-of-repo check is even reached.
- **Why it matters:** The subsystem makes an explicit guarantee — "Writing outside the active repo is blocked in web chat" (`chat_session_permissions.py:296`). A web-chat agent (confused or prompt-injected) can `Write`/`Edit` arbitrary files outside the repo, including the user's real `~/.claude/`, `~/.gobby/`, etc. config and plan files, since those directories are themselves plan-pattern matches.
- **Minimal fix:** In `find_out_of_repo_write_path`, move the `is_plan_file_path` exemption *after* the `resolved.is_relative_to(repo_root)` test (only exempt plan files inside the repo), and in the plan-mode branch require the resolved plan path to be inside `project_path` before allowing.
- **Confidence:** high — pattern behavior and both call sites verified against source.

### [IMPORTANT] Single-slot pending-approval state corrupts under concurrent tool approvals
- **Where:** `src/gobby/servers/chat_session_permissions.py:635-686` (`_wait_for_tool_approval`/`provide_approval`); same shape for `_pending_question` (`:312-335`) and `_pending_plan_event` (`chat_session.py:178-219`)
- **Failure mode:** `_pending_approval`, `_pending_approval_event`, `_pending_approval_decision` are single instance fields. When the SDK invokes `can_use_tool` for more than one tool concurrently in a turn, the second call overwrites `_pending_approval_event`; `provide_approval` sets only the newest event and both coroutines read the same `_pending_approval_decision`. A user's "approve" for tool A can authorize tool B, and the first call blocks to its 300s timeout (default reject). `send_message`'s `self._lock` does not serialize the SDK-driven `can_use_tool` path.
- **Why it matters:** Approval-decision integrity — a deny can become an allow for a different tool than the UI showed.
- **Minimal fix:** Key pending approvals/questions/plans by `tool_use_id` (a dict of `{id: (event, decision)}`) and have `provide_approval` target a specific id.
- **Confidence:** med — data structures unambiguously can't represent >1 concurrent approval; trigger depends on the SDK issuing parallel `can_use_tool` calls (confirm SDK behavior).

### [IMPORTANT] Plan mode does not block non-denylisted shell writes or non-gobby MCP write tools
- **Where:** `src/gobby/servers/chat_session_permissions.py:236-261` (plan-mode gate), `:374-375` (`_needs_tool_approval` returns `False` in plan mode); denylist `src/gobby/servers/chat_session_helpers.py:36-45`
- **Failure mode:** Plan mode hard-blocks only `{Edit, Write, NotebookEdit}` and *denylisted* write-bash, then auto-allows everything else (no approval prompt). So in plan mode a `Bash` write via a binary not in the denylist (`python -c "open('x','w')"`, `node -e`, `tee`, `dd`, `truncate`, `ln -s`, here-docs) runs, and an `mcp__gobby__call_tool` to a non-gobby write MCP server is auto-allowed.
- **Why it matters:** Plan mode advertises "research and design, not execute"; the denylist is trivially side-stepped, so an agent can mutate the repo while the user believes plan mode is read-only.
- **Minimal fix:** Gate shell + `mcp__gobby__call_tool` on a read-only allowlist in plan mode (deny by default), rather than denying only denylisted writes.
- **Confidence:** med-high — control flow traced; existing tests only cover denylisted commands.

### [IMPORTANT] WebSocket attach/observe subscribes to any named session with no authorization
- **Where:** `src/gobby/servers/websocket/handlers/session_observe.py:682-778` (`handle_attach_to_session` adds `session_message:session_id=…` / `hook_event:session_id=<external_id>` to `websocket.subscriptions` for any `session_id` the client names)
- **Failure mode:** Attach subscribes the connection to any session id with no check that the caller owns or may view it; the `external_id`-keyed hook subscription is derived from a DB row the caller does not own.
- **Why it matters:** Cross-session information disclosure (live message + hook streams) in any multi-client deployment.
- **Minimal fix:** Gate attach/observe on the connection's authenticated user/project scope before adding session-scoped subscriptions.
- **Confidence:** med — depends on the deployment trust model; nothing in these handlers enforces single-user/localhost.

### Stream & session lifecycle

### [BLOCKER] Interrupted / disconnected chat stream silently drops the partial assistant message (data loss)
- **Where:** `src/gobby/servers/websocket/chat/_streaming.py:68-173` (`_stream_chat_response`) — `persist_current_assistant`/`persist_done_metadata` run only via `_handle_done` (`_stream_events.py:301`); the `except asyncio.CancelledError`, `except (ConnectionClosed,…)`, `except Exception`, and `finally` blocks never persist
- **Failure mode:** Accumulated `assistant_blocks` are written to `chat_messages` only on a clean Done event. Every other exit discards them: `CancelledError` (stop button / a follow-up message before completion both call `_cancel_active_chat`) only sends an `interrupted=True` frame; all-clients-disconnect breaks the loop; a backend error sends `chat_error`; the `finally` only closes the generator and clears the active task. The user saw streamed text in the UI, but on reload it is gone.
- **Why it matters:** Interrupt is a *normal, frequent* path, so this is routine permanent data loss of visible assistant turns.
- **Minimal fix:** Persist accumulated content when the stream ends abnormally — call `await persistence.persist_current_assistant(session)` in `finally` (it no-ops when empty / already persisted, so it's safe after a Done).
- **Confidence:** high — `persist_current_assistant` has exactly two production callers, both in `_stream_events.py`; none run on the cancel/disconnect/error exits.

### [IMPORTANT] Interrupted / errored stream leaves DB session status stuck at "active"
- **Where:** `src/gobby/servers/websocket/chat/_streaming.py:131` sets `"active"`; the only reset to `"paused"` is `src/gobby/servers/websocket/chat/_stream_persistence.py:167` (called only from `persist_done_metadata` → `_handle_done`)
- **Failure mode:** On cancel/error/disconnect the status is never reset, so the session row stays `"active"` forever though no turn is running.
- **Why it matters:** UI "running" indicators, idle-session cleanup, dispatch/automation gates, and resume logic all key off status; combined with the data-loss Blocker, an interrupted turn loses the message *and* wedges status.
- **Minimal fix:** Reset status (`"paused"`) in the abnormal-exit paths, ideally in the same `finally` as the persist fix.
- **Confidence:** high

### [IMPORTANT] Cross-connection race on `track_active_task` orphans/duplicates a turn for one conversation
- **Where:** `src/gobby/servers/websocket/chat/_message_ingress.py:135-149` (`_cancel_active_chat` → `create_task` → `track_active_task`), `src/gobby/servers/websocket/chat/session_registry.py:75-80` (`active_tasks[conversation_id] = task`)
- **Failure mode:** WS messages serialize per-connection, but two connections sharing a `conversation_id` (two tabs / reconnect) run ingress concurrently with no per-conversation lock. The second `track_active_task` overwrites the map entry, leaving the first task untracked (un-cancellable, streaming to a possibly different socket) and corrupting `has_active_turn` accounting that gates compaction/wake.
- **Minimal fix:** Guard the cancel→create→track sequence with a per-conversation lock (reuse `_get_session_create_lock`), or have `track_active_task` cancel the task it overwrites.
- **Confidence:** med — requires two connections on one conversation.

### [IMPORTANT] `cleanup_idle_sessions` can tear out a session re-created mid-teardown
- **Where:** `src/gobby/servers/websocket/handlers/session_lifecycle.py:189-232`
- **Failure mode:** The reaper snapshots the stale set (safe) but awaits `_fire_session_end`/`_cancel_active_chat`/`session.stop()`/`run_db` per conversation; during those awaits a concurrent `_create_chat_session`/`continue_in_chat` can re-register the same `conversation_id`, and the later `_chat_sessions.pop(conv_id)` removes the freshly created session while `stop()` runs on the old one.
- **Minimal fix:** Re-check `last_activity`/a creation epoch immediately before `pop`, or hold the per-conversation create lock during teardown.
- **Confidence:** med

### [IMPORTANT] Idle reaper teardown diverges from clear/delete, leaking registry entries + queued tasks
- **Where:** `src/gobby/servers/websocket/handlers/session_lifecycle.py:66-131,134-186,189-232` — clear/delete use `web_chat_session_registry.unregister(...)` fallback; `cleanup_idle_sessions` always does `_chat_sessions.pop(...)` and never calls `registry.unregister`
- **Failure mode:** When a `web_chat_session_registry` is in use, the idle reaper removes the session from `_chat_sessions` but leaves a stale registry entry (and its queued tasks/locks).
- **Minimal fix:** Route the reaper's removal through the same `registry.unregister(...)` fallback.
- **Confidence:** med — confirm `web_chat_session_registry` is non-None in production wiring.

### [IMPORTANT] `stop()` does not release pending approval/plan/question waiters
- **Where:** `src/gobby/servers/chat_session.py:871-894` (`stop`); waiters at `chat_session_permissions.py:651`, `:318`, `chat_session.py:182`
- **Failure mode:** `stop()` disconnects the SDK client but never sets `_pending_approval_event`/`_pending_plan_event`/`_pending_answer_event`. A session stopped (idle cleanup, shutdown, `_reconnect_for_reasoning_effort_change`) while a waiter is parked leaves that coroutine blocked to its 300s/600s timeout, holding session + pending state; the new UI can no longer signal the stale event.
- **Minimal fix:** In `stop()` (or `_abort_pending()`), set every pending event with a default-deny decision before tearing down the client.
- **Confidence:** med — depends on whether the SDK cancels the in-flight `can_use_tool` task on disconnect.

### [IMPORTANT] Inter-session messages marked delivered before their context is injected (at-most-once loss)
- **Where:** `src/gobby/servers/websocket/chat/_pending_messages.py` (`_inject_pending_messages` calls `mark_delivered(msg.id)` while building the section, before `_fire_lifecycle` appends it)
- **Failure mode:** Messages are marked delivered during string assembly; if `_fire_lifecycle` fails, the event is blocked, or the turn is cancelled right after, the messages are already `delivered` and never re-injected. No rollback.
- **Why it matters:** Silent loss of P2P / command-result / web-chat piggyback messages — the exact payloads this path delivers.
- **Minimal fix:** Defer `mark_delivered` until the context is confirmed handed off / consumed.
- **Confidence:** med

### [IMPORTANT] `aclose` cleanup in `_drain_message_until_done` swallows `BaseException` (incl. `CancelledError`)
- **Where:** `src/gobby/servers/websocket/chat/session_registry.py:418-426` (`finally`: `except BaseException: pass`)
- **Failure mode:** Closing the stream generator in `finally` swallows all `BaseException`, so when the surrounding queued-compaction/wake task is cancelled, `await close_result` raising `CancelledError` is suppressed — cancellation of `_run_queued_after_turn` may not unwind.
- **Minimal fix:** Catch `Exception`, not `BaseException`; let `CancelledError`/`KeyboardInterrupt` propagate.
- **Confidence:** high

### Chat backends (subprocess streaming)

### [BLOCKER] Droid session generator swallows `asyncio.CancelledError`, corrupting interrupt/disconnect
- **Where:** `src/gobby/servers/websocket/chat/backends/droid.py:367-374` (`except (RuntimeError, OSError, ConnectionError, asyncio.CancelledError)` then yields a fake error + Done, never re-raises); contrast `acp_session.py:184` / `codex.py:899` which catch `Exception`
- **Failure mode:** On stop/new-message, `_cancel_active_chat` calls `session.interrupt()` then `active_task.cancel()`, throwing `CancelledError` into the active `await`. Because this `except` catches `CancelledError` (a `BaseException`), cancellation is swallowed: the generator emits "Generation failed: …" to the client and returns normally, bypassing the streaming layer's clean `interrupted=True` path (`_streaming.py:145-156`) and triggering `RuntimeError: async generator ignored GeneratorExit` at the next suspension.
- **Why it matters:** Every Droid interrupt and disconnect is mishandled (fake error instead of clean interrupt; broken cooperative-cancellation contract).
- **Minimal fix:** Drop `asyncio.CancelledError` from the tuple (let it propagate); if cleanup-on-cancel is needed, add `except asyncio.CancelledError: raise` before the broad handler or use `try/finally`.
- **Confidence:** high

### [IMPORTANT] Untrusted Droid `permission_request` records crash the parser via `TypeError`
- **Where:** `src/gobby/servers/websocket/chat/backends/droid_stream.py:14-17` (`_content_delta(kind, **data)`), called with `**record`/`**block` after popping only `"type"` in both `permission_request` branches
- **Failure mode:** Only `"type"` is stripped before the `**data` spread. A `permission_request` record carrying a `"kind"` key → `_content_delta("permission_request", kind="x", …)` → `TypeError: multiple values for 'kind'`. `parse_droid_stream_line` only guards `json.loads`, not record translation, so the `TypeError` aborts the whole turn (or session init) instead of skipping the line like every other malformed record.
- **Minimal fix:** Strip reserved keys (`kind`, `content`, `id`, …) before spread, or wrap `_stream_events_from_droid_record` in the same try/except as the JSON decode.
- **Confidence:** high (mechanics) / med (whether Droid emits a `kind` field).

### [IMPORTANT] Codex `send_message` loop has no liveness check — hangs forever if the app-server dies mid-turn
- **Where:** `src/gobby/servers/websocket/chat/backends/codex.py:765-768` (`while not turn_completed.is_set(): … wait_for(queue.get(), 0.1) / except TimeoutError: continue`)
- **Failure mode:** The loop exits only on a `turn/completed`/`thread/closed` notification. If the shared codex subprocess crashes after `start_turn`, those never arrive and the loop spins on the 0.1s timeout indefinitely; there's no `is_connected` check and no overall turn deadline, so the generator never returns.
- **Minimal fix:** Break/raise when `self._client is None or not self._client.is_connected`, or bound the turn with `asyncio.timeout`.
- **Confidence:** med — depends on the client exposing a liveness signal.

### [IMPORTANT] Broad `except Exception` in the streaming generators converts real bugs into assistant text
- **Where:** `src/gobby/servers/websocket/chat/backends/acp_session.py:184-193`, `codex.py:899-908`, and the Droid handler above
- **Failure mode:** The loops catch every `Exception`, log, then `yield TextChunk("Generation failed/Error: {exc}")` + Done. Programming errors (KeyError/AttributeError/bad event shapes) become a user-facing "successful" turn instead of routing through `_streaming.py`'s `_classify_chat_error`/`chat_error` channel.
- **Minimal fix:** Narrow to expected runtime/connection types, or re-raise after logging so the streaming layer classifies it.
- **Confidence:** med — logged, so contract-drift/maintainability with correctness impact, not a hard crash.

### Voice / tmux

### [BLOCKER] Attached-session TTS pipeline + offset map leak on detach / disconnect
- **Where:** create at `src/gobby/servers/websocket/voice_attached.py:46-115` (`_active_tts_pipelines[session_id]`, `_attached_tts_offsets[{session_id}:{message_id}]`), cleanup only on `complete=True` (`:114-115`); detach `src/gobby/servers/websocket/handlers/session_observe.py:941-977` and disconnect `src/gobby/servers/websocket/server.py:241-247` never touch them
- **Failure mode:** Attaching to a CLI session with voice on creates a `TTSPipeline` (with a worker task blocked on `_queue.get()`) and accumulates per-message offsets. These are freed only on a completion event. Detaching or disconnecting before completion — the normal case for closing a tab mid-stream — leaves the pipeline in `_active_tts_pipelines`, the worker task alive (holding the TTS model), and `_attached_tts_offsets` growing unboundedly over the daemon's lifetime.
- **Why it matters:** Leaked asyncio task + model reference + unbounded dict growth per attached-voice session — resource leak under normal use.
- **Minimal fix:** Add a `cleanup_attached_session_tts(session_id)` helper (cancel the pipeline, purge `{session_id}:` offset entries) invoked from both detach and the disconnect `finally`.
- **Confidence:** high — no purge of `_attached_tts_offsets` exists anywhere; detach/disconnect never touch `_active_tts_pipelines`.

### [IMPORTANT] Untracked fire-and-forget `create_task(existing.cancel())` can be GC'd mid-cancel
- **Where:** `src/gobby/servers/websocket/voice/mixin.py:151` (`asyncio.create_task(existing.cancel())`, not stored)
- **Failure mode:** The event loop holds only a weak reference to bare tasks, so the cancel coroutine can be GC'd before completing, leaving the old pipeline's worker task running. The sibling `_spawn_background_task` (`warmup.py:40-50`) exists precisely to prevent this.
- **Minimal fix:** Use `self._spawn_background_task(existing.cancel(), name="cancel-tts-pipeline")`.
- **Confidence:** high

### [IMPORTANT] No per-client voice cleanup / idle model-unload on disconnect
- **Where:** disconnect `finally` `src/gobby/servers/websocket/server.py:241-247` never calls `_check_voice_idle` (`src/gobby/servers/websocket/voice/warmup.py:384-437`)
- **Failure mode:** `_check_voice_idle` unloads STT/TTS models only when `len(_chat_sessions) == 0`, but nothing calls it on client disconnect, so warmed models (hundreds of MB–GB) stay resident until full daemon shutdown.
- **Minimal fix:** Schedule `await self._check_voice_idle()` in the disconnect `finally` (voice-enabled guard).
- **Confidence:** med — confirm no chat-session-removal site already triggers it.

### [IMPORTANT] tmux `set-option`/`refresh-client` bypass config isolation (`-f /dev/null`) and WSL handling
- **Where:** `src/gobby/servers/websocket/tmux.py:330-360` (`_handle_tmux_attach`), `:556-567` (`_handle_tmux_resize`), `:586-595` (`_handle_tmux_refresh_client`) build argv by hand instead of via `TmuxSessionManager._base_args()` (`session_manager.py:73-98`, which always adds `-f /dev/null` and WSL prefix)
- **Failure mode:** These calls omit the `-f /dev/null` config isolation, so on a host with `destroy-unattached on` in `~/.tmux.conf` they re-introduce exactly the detached-session-kill failure `_base_args` guards against; on WSL they invoke a missing `tmux` and fail silently.
- **Minimal fix:** Route these through `TmuxSessionManager` (`set_option`/`refresh_client` methods using `_base_args`).
- **Confidence:** high

### HTTP bootstrap & middleware

### [IMPORTANT] CORS middleware runs innermost, so auth-rejected and error responses lose CORS headers
- **Where:** `src/gobby/servers/app_factory.py:512-534` (CORS added first → outermost-reversed → innermost; Auth added last → outermost)
- **Failure mode:** Starlette wraps `reversed(user_middleware)`, so execution is `Auth → ProjectContext → Telemetry → CORS → router`. A 401 short-circuit in `AuthMiddleware` (and OPTIONS preflight for a protected path) is produced outside `CORSMiddleware`, so it carries no `Access-Control-Allow-Origin`; cross-origin clients see a CORS error instead of the real status.
- **Minimal fix:** Add `CORSMiddleware` last so it is outermost.
- **Confidence:** high — verified against installed Starlette `add_middleware`/`build_middleware_stack` semantics; impact bounded to genuinely cross-origin clients (bundled UI is same-origin).

### [IMPORTANT] ProjectContextMiddleware runs synchronous DB lookups on the event loop
- **Where:** `src/gobby/servers/middleware/project_context.py:51-101` (`dispatch` → sync `_set_context` → `session_manager.get` + `LocalProjectManager(...).get`)
- **Failure mode:** For every request carrying `x-gobby-session-id`/`x-gobby-project-id` (i.e. essentially all UI/API traffic), two blocking Postgres lookups run on the loop, serializing it per request. Distinct from the auth-PBKDF2 path already filed under the routes review.
- **Minimal fix:** Make `_set_context` async and bridge the DB calls via `server.run_db`/`asyncio.to_thread`.
- **Confidence:** high

### [IMPORTANT] Global artifact broadcaster is set on startup but never reset on shutdown
- **Where:** `src/gobby/servers/app_factory.py:285-292` sets `set_artifact_broadcaster(...)`; the shutdown block (`:384-485`) never clears it
- **Failure mode:** The broadcaster closure captures `server` (and its websocket server). Any process that builds more than one lifespan (test harness, in-process restart) keeps the stale closure registered in module-global state, routing artifact events to a torn-down server and preventing GC.
- **Minimal fix:** Call `set_artifact_broadcaster(None)` in the shutdown section.
- **Confidence:** high — only the startup setter exists in production; no reset on shutdown.

### [IMPORTANT] `_handle_message` assumes the inbound payload is a JSON object
- **Where:** `src/gobby/servers/websocket/server.py:262-263` (`data = json.loads(message); msg_type = data.get("type")`)
- **Failure mode:** `json.loads` of attacker-controlled text accepts any JSON value; `"5"`/`"true"`/`"[]"` make `data.get` raise `AttributeError`. It degrades to a generic "Internal server error" today, but every downstream handler does raw `data.get(...)` on an assumed dict with no type guard at the dispatch boundary.
- **Minimal fix:** `if not isinstance(data, dict): await self._send_error(...); return` right after `json.loads`.
- **Confidence:** high

### Handlers — sync DB on the WebSocket event loop

### [IMPORTANT] `check_resume_blocked` / `merge_variables` / `handle_set_worktree` block the WS event loop
- **Where:** `src/gobby/servers/websocket/handlers/session_observe.py:651,664` (`db.fetchone` ×2 in `check_resume_blocked`, on every `continue_in_chat`); `src/gobby/servers/websocket/handlers/session_config.py:298,84` (`svm.merge_variables` → `transaction_immediate` on every `set_mode`); `:415-417,426` (`wm.get(worktree_id)` + `os.path.isdir(worktree_path)` on every `set_worktree`)
- **Failure mode:** These `async` handlers call synchronous storage methods (and one blocking `os.path.isdir` on a client-supplied path) directly on the loop. The module's own convention is `await run_db(mixin, fn, …)` (used two lines away for `update_chat_mode`), so these are inconsistent. A blocking query on the WebSocket loop stalls *every* connected client's stream, ping, voice, and tmux I/O — a larger blast radius than the per-request HTTP case.
- **Minimal fix:** Route the DB calls through `run_db` and `os.path.isdir` via `asyncio.to_thread`.
- **Confidence:** high (recorded as IMPORTANT for severity-calibration consistency with the routes-layer systemic finding; the WS-loop blast radius makes these the highest-impact IMPORTANTs here).

### Pending interactions

### [IMPORTANT] Timeout/response race can deliver `timeout` to a waiter whose interaction was actually resolved
- **Where:** `src/gobby/servers/pending_interactions.py:166-191` (`resolve`), `:209-219` (`expire`), `:327-335` (`_timeout_handler`), `:221-225` (`_wake_waiter`)
- **Failure mode:** `resolve` and `expire` are serialized only by the DB `WHERE status='pending'` guard, which protects the row, not the in-memory future. When a user response and the timeout fire in the same window, `expire`'s DB no-op can still reach `_wake_waiter(id, {"decision":"timeout"})` and win the (not-yet-done) future before `resolve`'s wake, so the waiter sees `timeout` while the DB row says `resolved`. The `not future.done()` guard prevents a crash but not the wrong decision.
- **Why it matters:** A human approval submitted right at the timeout boundary is silently dropped; the agent proceeds as if the user timed out.
- **Minimal fix:** Have `_expire_pending` return rowcount (like `_resolve_pending`) and only wake `timeout` when the expire write actually won; or gate resolve/expire with a per-interaction `asyncio.Lock`.
- **Confidence:** med — paths confirmed; a concurrent resolve+timeout test would confirm the window.

### Misc correctness

### [IMPORTANT] Unbounded file read in the single-file diff path
- **Where:** `src/gobby/servers/session_changes.py:76-95` (`_new_file_diff` does `abs_path.read_text()` with no size cap, then builds a `"+line"` string per line), reached via `:235` (`compute_session_file_diff`)
- **Failure mode:** A new/untracked multi-GB file in the workspace is read fully into memory and roughly doubled when building the diff. (Offloaded via `to_thread`, so the loop isn't blocked, but memory is unbounded.) The 10s git timeout doesn't apply to this Python read.
- **Minimal fix:** Cap bytes via `os.path.getsize`/partial read and emit a "too large" diff above the threshold.
- **Confidence:** high

### Provider model catalogs

### [IMPORTANT] Four Droid models ship with no `context_length` (catalog drift)
- **Where:** `src/gobby/servers/provider_model_defaults.py:139-156` (`minimax-m2.7`, `minimax-m2.5`, `kimi-k2.6`, `kimi-k2.5`), absent from both `_DROID_PROVIDER_CATALOG_CONTEXT_LENGTHS` and `_STATIC_CONTEXT_LENGTHS` in `src/gobby/llm/context_windows.py:72-126`
- **Failure mode:** `get_context_window("droid", "minimax-m2.7")` returns `None` while all 20 sibling Droid models resolve a value, so consumers fall back to a generic default that is wrong for a 192k–256k-class model.
- **Minimal fix:** Add the four IDs with their published context limits.
- **Confidence:** high — reproduced; the presence test for these IDs never asserts a context length.

### [IMPORTANT] Droid context resolution can pick the generic static window over Droid's authoritative one
- **Where:** `src/gobby/servers/provider_models.py:354-364` (`get_context_window_with_source`, `droid` branch)
- **Failure mode:** For a shared ID (e.g. `gpt-5.4`), the underlying-provider lookup at `:360-363` uses `include_static=True`, so an empty codex cache falls through to the generic static `258_400` and short-circuits before the Droid-specific `200_000` at `:364` is consulted.
- **Minimal fix:** Call the underlying-provider lookup with `include_static=False` so only live values win and the Droid catalog takes precedence.
- **Confidence:** med — requires the Droid catalog row absent from `self._providers` (pre-/partial-refresh window).

## Nits

### [NIT] Dead `_DANGEROUS_BASH_PATTERNS` — two unused copies
`src/gobby/servers/chat_session_permissions.py:100-107` (+ `_is_dangerous_bash`/`_is_write_mcp_call`/`_mcp_call_tool_key`, referenced only by tests; stale "used by accept_edits mode" comment) and a second unused copy at `src/gobby/servers/websocket/chat/permissions.py:88-93`. Delete or wire into the accept_edits path. Confidence: high.

### [NIT] Global exception handler returns HTTP 200 for all unhandled exceptions
`src/gobby/servers/exception_handlers.py:27-77` — intentional for hook ingress, but applies to all routes, masking 500s as 200 for UI/API clients and monitoring. Scope the 200-on-error to hook paths. Confidence: high (behavior) / med (defect vs design).

### [NIT] Auth public-prefix matching has no path boundary
`src/gobby/servers/middleware/auth.py:32-50` — `startswith("/api/mcp")`/`"/api/admin/config"` would match a future `/api/mcpx` or `/api/admin/config/write` as public. Require exact or trailing-slash match. Confidence: high (no live bypass today).

### [NIT] `broadcast` `json.dumps` outside the per-client error handling
`src/gobby/servers/websocket/broadcast.py:104` — a non-serializable payload raises before the per-client loop, crashing the originating handler. Wrap with try/except or `default=str`. Confidence: med.

### [NIT] `_proxy_websocket` may `close()` a never-accepted client socket
`src/gobby/servers/app_factory.py:737-742` — relies on a swallowing inner `except`. Track an `accepted` flag. Confidence: med.

### [NIT] Broad `except Exception` swallows in handlers / readers
`src/gobby/servers/websocket/handlers/core.py:346-352` (terminal input), `chat/backends/codex.py:245-275,284-308` (`_stat_size`/`_read_assistant_text` catch only `OSError`, so a parse error escapes to the turn handler). Narrow to expected types. Confidence: high / med.

### [NIT] Pending-config dicts leak entries for abandoned conversations
`src/gobby/servers/websocket/handlers/session_config.py:307` (`_pending_modes`/`_pending_projects`/`_pending_providers`/`_pending_agents`/`_pending_worktree_paths`) are popped only at session creation; a client that sets a mode then disconnects without sending a message leaves the entry forever. Sweep in idle cleanup. Confidence: med.

### [NIT] `handle_send_to_cli_session` constructs `InterSessionMessageManager` per message
`src/gobby/servers/websocket/handlers/session_observe.py:781-938`. Reuse an instance. Confidence: high.

### [NIT] Malformed `tmux_resize` payload raises an unguarded `ValueError`
`src/gobby/servers/websocket/tmux.py:552` — `int(rows)` on socket input raises for non-numeric, logging a full traceback for what the handler treats as a silent no-op. Wrap in try/except. Confidence: high.

### [NIT] Synchronous settings-file reads on the loop in Qwen warmup
`src/gobby/servers/websocket/chat/local_openai_warmup.py:420-432` (`resolve_qwen_local_openai_target` → `read_text` at `:58`), called from an async backend. Offload via `asyncio.to_thread`. Confidence: high (sync I/O) / low (tiny files).

### [NIT] TTS metadata + binary sent as two separate awaited sends
`src/gobby/servers/websocket/voice/tts.py:158-167` — metadata send can succeed while the binary send hits a swallowed `ConnectionClosed`, leaving inconsistent frame accounting. Skip a client for the chunk once any send fails, or combine frames. Confidence: med.

### [NIT] Droid command construction interpolates `--cwd`/`--model`/`--reasoning-effort` without validation
`src/gobby/servers/websocket/chat/backends/droid.py:530-556` — `create_subprocess_exec` (no shell), but a `_model` beginning with `-` is argument-injection-adjacent and `cwd` isn't resolved/existence-checked (ACP does both at `acp.py:152`). Validate/allowlist. Confidence: med (no confirmed client-controlled path).

### [NIT] Abandoned managed-backend conversations keep a child process alive
`src/gobby/servers/websocket/chat/backends/droid.py:537-545` spawn; teardown only via `clear_chat`/daemon stop. No idle-eviction observed for managed backends. Add an idle sweep. Confidence: low — confirm no eviction job exists.

### [NIT] `_classify_chat_error` keys off substring matching of error text
`src/gobby/servers/websocket/chat/_streaming.py:54-66` — `"auth"`/`"connection"`/`"timeout"`/`"429"` substrings mislabel benign messages. Prefer type checks. Confidence: high (cosmetic).

### [NIT] `_discover_provider_models` "agy" branch is unreachable
`src/gobby/servers/provider_models.py:550-551` — `agy` has `live_model_discovery=False`, so it never reaches this branch. Drop it. Confidence: high.

### [NIT] `create_provider_model_catalog`/`ProviderModelCatalog` silently discard `daemon_config`
`src/gobby/servers/provider_models.py:225-247` — the param is accepted but inert, and `agents/reasoning.py:94-104` probes three constructor shapes catching `TypeError`. Wire it through or remove it. Confidence: high (NIT — documented as reserved).

### [NIT] `grok.models_from_cache` has unguarded raising I/O
`src/gobby/servers/provider_models_grok.py:76-92` — `read_text`/`json.loads` throw on missing/corrupt cache; both current callers wrap in `except Exception`, but a future caller would crash. Catch `(OSError, json.JSONDecodeError)` internally. Confidence: high.

### [NIT] Files approaching the monolith cap
`src/gobby/servers/chat_session.py` (957), `src/gobby/servers/provider_models.py` (893) — under the 1,000-line cap but trending; the per-provider `_discover_*` helpers and the ~190-line `send_message` are natural extraction seams. No action required now. Confidence: high.

## Systemic patterns

1. **Synchronous DB / FS / crypto on the event loop, with the off-loop bridge applied inconsistently.** The WebSocket server loop and the request-middleware chain both run blocking work directly: `check_resume_blocked`/`merge_variables`/`handle_set_worktree` (sync DB + `os.path.isdir`), `ProjectContextMiddleware` (two sync lookups per request), and the auth-middleware PBKDF2 chain (filed under routes). `run_db`/`asyncio.to_thread`/`_spawn_background_task` exist and are used correctly *in the same files* as the violations. On the WS loop the blast radius is every connected client's live stream — the highest-impact class here.

2. **Stream/session lifecycle is finalized only on the happy-path "Done" event.** Assistant-message persistence and the `"active"→"paused"` status reset live solely in `_handle_done`; attached-session TTS pipelines and offset maps are freed only on `complete=True`; pending approval/plan/question waiters are released only by `provide_*`. Every abnormal exit (cancel, disconnect, error, idle reap) skips all of it, producing the correlated data-loss + stuck-status Blocker, the TTS-leak Blocker, and the stop()-leaves-waiters IMPORTANT. The fix shape is a single guaranteed finalization step (in `finally`) per lifecycle that persists, resets status, and tears down resources regardless of how the turn ended.

3. **Cancellation is handled incorrectly or swallowed across the three streaming backends and the registry drain.** Droid lists `asyncio.CancelledError` in its `except` tuple (the backend Blocker) while ACP/Codex correctly let it escape; `_drain_message_until_done`'s `finally` catches `BaseException`. The identical "yield an error TextChunk + Done and return" recovery shape is copy-pasted across `droid.py`/`acp_session.py`/`codex.py`, which is why one variant silently broke cancellation. A shared "stream-with-recovery" base helper would remove the drift.

4. **Shared mutable maps (`active_tasks`, `_chat_sessions`, the registry) are mutated across awaits without a per-conversation turn lock.** Session *creation* is guarded by `_session_create_locks`, but turn lifecycle (track/overwrite, cancel-then-recreate) and idle teardown are not, yielding the cross-connection turn race, the idle-reaper-vs-create race, and the registry/`_chat_sessions` teardown asymmetry. Unify the two mutex regimes or give the turn path its own per-conversation guard.

5. **Denylist-based safety where an allowlist is required.** Both the plan-mode write block (`_BASH_WRITE_PATTERNS`) and the out-of-repo exemption (`_PLAN_FILE_PATTERN`) decide "is this dangerous?" by string-matching rather than "is this provably safe / inside the repo?" The same pattern is reused as both a permission signal and a path-containment signal without ever resolving against `project_path` — the root of the security Blocker. Prefer resolve-then-contain and explicit read-only allowlists.

6. **Untrusted inbound shapes are trusted at the boundary.** `_handle_message` doesn't assert `isinstance(data, dict)`; the Droid stream parser spreads untrusted records into a reserved-keyword function; handlers do raw `body.get`/`int()` on socket input. Per-field validation is uneven. A dict-guard at dispatch plus reserved-key stripping in parsers would harden the surface.

7. **Catalog/static-map drift in provider models.** Model IDs are enumerated in three+ maps with no cross-check that every shipped model resolves a context length; the multi-tier resolver (catalog → underlying → static) is untested for the empty-underlying-cache and unseeded-catalog states, exactly where it returns the wrong generic window. A startup assertion that every `DROID_MODEL_CATALOG` entry resolves a non-`None` window would catch the class.
