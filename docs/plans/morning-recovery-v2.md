# Overnight overload recovery: gclient close-target, stale-epoch attach, stuck run, worktree recovery

## Context

On the night of 2026-09-19/20 the daemon became unresponsive twice (01:31-01:56 and 04:48-05:02) and the machine was shut down at 05:28 and rebooted at 05:29. The daemon never crashed on its own: two coordinator sessions (gobby#13983, gobby#13940) fanned out 11 codex `backend-developer` workers that ran 11-wide for over four hours, cargo cold-rebuilt several isolated worktrees at once (first build after commit 90b4bf11cc), and the daemon amplified the load with uncapped fan-out (one `gcode` subprocess per file in the code-index sync worker retrying 3x per file against a dead LM Studio endpoint; one thread plus process per daemon git call; pane capture per session every 5 s). Memory was never the constraint. The daemon was also restarted ten times between 20:24 and 05:03 while those agents held live runs.

Investigation evidence (logs, hub DB, source) established the following still-broken state after the reboot:

1. **gclient "close terminal" on a session row kills the focused pane.** `agent_items` in `crates/gclient/src/app/live_loop/menu.rs` emits `MenuAction::Act(Action::CloseTerminal)`; the handler in `crates/gclient/src/app/live_loop/actions.rs:410` acts on `chrome.focused_pane()`, not the row's pane. Closing row gobby#13991 killed the operator's own Claude session (terminal 778761b2, pid 99173, at 05:56:46). "take control" in the same menu has the same shape.
2. **Ghost terminals with the pre-reboot host epoch are still attachable.** 64 `terminals` rows are `state='exited'` with epoch `7fb98250…`; the live gterm epoch is `32bd40db…`. `NativeTerminalRuntime.attach_locator` (`src/gobby/terminals/native_runtime.py` ~767) prefers the stored row epoch and never compares it to the live one, so every attach to ca20fcfb (13991) raises `HostEpochChangedError` in `frame_client.handshake` and `terminal_ws._start_proxy_attach` reports it as retryable `proxy_start_failed`. `TerminalStore.attach_locator` (`src/gobby/storage/terminals.py` ~657) already rejects the mismatch; the websocket path does not use it.
3. **Stuck triple.** Run `c7d74616` is `running`, session 13991 is `active`, task #22561 is claimed by it, close review `61d2cd87` is `error`. At boot `_cleanup_missing_terminal_agent_run` (`src/gobby/runner_lifecycle_reconcile.py:484`) left the run to task reconciliation because the review was still active; periodic `check_completed_task_agents` (`src/gobby/agents/lifecycle_reconciliation.py` ~238) returns nothing because `_caller_session_is_live` sees the `active` session; the session never expires because expiry waits for the run; the liveness monitor only inspects tmux targets and PIDs, never an exited native row. Nothing self-heals.
4. **Duplicate heuristic titles.** Every `backend-developer` codex session was titled "Lifecycle model Rules authored": the heuristic titler took lines 39-40 of `src/gobby/install/shared/workflows/agents/backend-developer.yaml`.
5. **Uncommitted agent work** in six worktrees under `~/.gobby/worktrees/gobby/`: 22562 and 22563 fully staged with zero commits; 22565 unstaged; 22593 untracked `crates/gcode/src/communities/remap.rs`; 22567 and 22602 modified on top of commits. Sessions 13990/13991/13993/13994 hold handoffs describing intent.
6. **Stale claims.** #22405 claimed by expired session 13987 (escalated); #22567 open with `validation_status='invalid'`.
7. **No agent concurrency cap exists** in config.

User decisions (2026-09-20): do the gclient fix, the daemon fixes with one restart, and unstick 13991. The daemon restart must wait until the game-goblins `replenishment-daily` cron (lock `/tmp/game-goblins-lightspeed-sync.lock`, `ops/run-job.sh` pids 88064/88273) finishes. **Worktree recovery (22562/22563/22565/22567/22593/22602) is owned by the user's separate codex agent and is out of scope here**; this plan must not touch those worktrees or respawn workers into them. Nightly feedback reviewers on gpt-5.6-sol/medium are correct and out of scope.

Further decisions (2026-09-20): no host-wide agent-run cap in this batch; file a `needs-decision` task for a configurable cap in `spawn_agent`'s `_spawn_guards.py` instead.

**Execution model (user decision, supersedes the earlier "codex agent owns recovery" split):** this session is the coordinator. All implementation, both the fixes (streams A and B) and the recovery of the interrupted tasks, is done by spawned codex `backend-developer` subagents with `provider="codex"`, `model="gpt-5.6-sol"`, `reasoning_effort="xhigh"` (explicit instruction for this plan; it overrides the standing grok preference in memory 9305e121). The interrupted codex threads are not resumed with `codex resume`; `docs/plans/morning-recovery.md` is used only for its audit and ordering. One implementation subagent runs at a time and its close validator finishes before the next spawn (last night's overload, and the codex plan's rule). The coordinator does the MCP-only steps itself: unsticking 13991, task creation, waits, landing, the announced restart. #22622 is finished before the restart (its files do not overlap the daemon fixes), which removes the ordering deadlock with the #22567 dirty-template guard.

Route: five independently closeable code outcomes plus operational steps. Each fits one focused session, so this uses the tasks workflow (one Gobby task per outcome), not a registered `.gobby/plans` artifact.

Monolith ceiling watch (hand-maintained files must stay under 1,000 lines): `menu.rs` 913, `actions.rs` 978, `app/mod.rs` 927, `native_runtime.py` 850, `terminal_ws.py` 794.

## Work stream A: gclient close-target fix (one task, no daemon restart)

Precise root cause: `apply_live_menu_action` (`crates/gclient/src/app/live_loop/actions.rs:288`) already calls `focus_menu_target` before any `MenuAction::Act`, and for an agent row that runs `reveal_agent` (`live_loop/projects.rs:91`). `chrome.focus_pane` only succeeds synchronously when the pane is in the active project's tab set; otherwise `place_live_terminal` sends a `WorkspaceOp::TabCreate` to the daemon and returns, focus moves only when the `tab.created` event is projected later, and `Action::CloseTerminal` reads the still-focused operator pane. An agent-spawned session the operator never opened is exactly that case. `take control` / `release control` in the same menu share the flaw. The existing test `closing_a_bare_terminal_row_kills_that_row_not_the_focused_pane` passes only because `show_roster` puts every roster pane on the tab bar.

Design: pane-carrying menu actions, following the existing `SwapWithFocused(PaneId)` / `TogglePassthrough(PaneId)` pattern. Awaiting the daemon placement inside `focus_menu_target` would block the loop and contradict the click-never-waits design.

1. `crates/gclient/src/app/live_loop/menu.rs` (913 lines, ~+12 production lines):
   - Add `MenuAction::CloseTerminal(PaneId)`, `TakeControl(PaneId)`, `ReleaseControl(PaneId)`.
   - `control_item(state, pane)` emits `ReleaseControl(pane)` when held, else `TakeControl(pane)` (the take-back arm collapses into take; label unchanged). Update the `pane_items` call.
   - `agent_items`: `|pane| control_item(ws.pane(pane), pane)`; the non-orphan close arm becomes `CloseTerminal(pane)` when the row has a pane, still wrapped in `enabled_if(.., pane.is_some())`. The `Act` placeholder survives only for the no-pane row, which `activate_menu` never dispatches; say so in a one-line comment.
2. `crates/gclient/src/app/live_loop/projects.rs` (794 lines): new `pub(super) async fn close_live_terminal(workspace, chrome, pane_id)`. External pane: if `chrome.focus_pane(pane_id)` succeeds, `close_live_pane` (detach path unchanged); else `release_live_control(workspace, pane_id)` (idempotent). Otherwise `terminate_live_terminal(workspace, pane_id)` then `sync_live_chrome`. Never consults the focused pane. Import `close_live_pane` from `super::actions` and `release_live_control` from `super::control`. Lives here because actions.rs is at 978 lines.
3. `crates/gclient/src/app/live_loop/actions.rs`: the `Action::CloseTerminal` arm becomes `if let Some(pane_id) = chrome.focused_pane() { close_live_terminal(..).await? }` (comment moves to the helper, net -9 lines). In `apply_live_menu_action`, before the `_ =>` fallback: `MenuAction::CloseTerminal(pane) => close_live_terminal(..)` with no `focus_menu_target` (closing must not send a `tab.create` for a terminal about to die); `TakeControl(pane)` and `ReleaseControl(pane)` keep `focus_menu_target` then call `take_live_control(workspace, pane)` / `release_live_control(workspace, pane)`.
4. `crates/gclient/src/app/run_loop.rs`: `apply_local_menu_action` has a `_ => false` fallback, so nothing breaks; optionally route the two control variants to `scripted_focus(.., pane, true)` for parity.

Tests:
- menu.rs unit tests: update `menus_list_items_per_target_and_state` and `row_menus_list_items_per_target_and_state` to the new variants; in the row fixture (held focused, blocked unfocused) add `assert_ne!(chrome.focused_pane(), Some(blocked))` beside the `CloseTerminal(blocked)` assertion. New `agent_row_close_activates_with_the_row_pane_not_the_focused_one`: open the Agent menu for `run:term-blocked`, select "close terminal", `activate_menu` returns `MenuAction::CloseTerminal(blocked)` while `chrome.focused_pane() == Some(held)`.
- `crates/gclient/tests/client_loop.rs`: new `closing_an_unshown_agent_row_kills_that_row_not_the_focused_pane`, cloned from `closing_a_bare_terminal_row_kills_that_row_not_the_focused_pane`: seed the workspace with a daemon tab holding only terminal-a, roster lists terminal-a and terminal-b, do not call `show_roster`, right-press the terminal-b row, left-press "close terminal", `wait_for_websocket_requests(mock, "terminal_kill", 1)`. Assert kills == ["terminal-b"], terminal-a's pane survives and stays focused, no `tab.create` op, terminal-b's pane is gone. Without the fix it kills terminal-a. Fetch memories 679bf344 (loop-test ordering) and bdf897bf (sidebar row click hit areas) before writing it.

Commands: `cargo fmt -p gobby-client`, `cargo clippy -p gobby-client --all-targets`, `cargo nextest run -p gobby-client`. Load the `rust` skill and `crates/CLAUDE.md` first.

Install: commit first (the stale marker compares binary mtime with the last commit touching `crates/gclient` and `crates/gcore`), then `uv run gobby install --no-interactive` (runs `install_gclient_from_submodule`: cargo build, sha compare against `~/.gobby/bin/.gclient-source-sha256`, promote via `stage_and_promote_binary_file` on a new inode; not part of the stamped gcode/gdaemon/ghook set, so no `mixed installed binary set` risk). Verify `uv run gobby status` shows gclient without `[stale: rebuild and install]`. The running gclient (pid 1811) keeps the old inode; the user relaunches gclient when convenient.

## Work stream B: daemon fixes (epic with four leaves, one restart)

All Python, no crate or web build. gclient's `attach_refusal_is_transient` (`crates/gclient/src/app/live_attach.rs:15`) is an allowlist (`host_not_ready | host_open_timeout | proxy_start_timeout`), so new refusal codes are non-transient with zero client change; the web hook just renders `reason`. Ceiling watch: `src/gobby/agents/spawn_executor.py` is 982 lines, add nothing there.

Order: B2 (unblocks future stuck triples) -> B1 (stops attach churn) -> B4 -> B3. Each leaf: claim task, implement, targeted tests, `uv run ruff format`, `uv run ruff check src/`, `uv run mypy src/`, commit `[gobby-#NNNNN] fix: …`, close with commit sha. Then one restart (stream C2), then close the epic with the post-restart checks as evidence.

### B1. Native attach validates the live host epoch and refuses dead rows

Root cause: `NativeTerminalRuntime.attach_locator` (`src/gobby/terminals/native_runtime.py:767-778`) trusts `terminal.host_epoch` and never compares it to the live epoch, so the mismatch is only found inside the frame handshake and surfaces as the generic `proxy_start_failed`. For a native row the stored epoch is the identity being validated; the live host is the reference and the row is only the fallback when the host reports none (same shape as `_live_host_epoch` in `src/gobby/servers/routes/terminals.py:129`). `TmuxTerminalRuntime.attach_locator` is deliberately unchanged (tmux panes survive a host respawn; memory e4e0998b covers that case).

1. `src/gobby/storage/terminals.py`: extract the native branch of `TerminalManager.attach_locator` (~660-670: the `row.host_epoch != live_host_epoch` check raising `HostEpochMismatchError` plus the `AttachLocator(backend="native", …)` build) into module-level `native_attach_locator(row, *, live_host_epoch, host_socket)`. The manager calls it; single copy of the check.
2. `src/gobby/terminals/native_runtime.py` `attach_locator`: `live = str(getattr(self._client, "host_epoch", "") or "") or str(terminal.host_epoch or "")`, then `return native_attach_locator(terminal, live_host_epoch=live, host_socket=… frames_socket_path(directory) …)`; delete the inline construction.
3. `src/gobby/servers/websocket/terminal_ws.py`: add to `PROXY_ATTACH_FAILURE_REASONS` `terminal_exited` ("terminal row is exited or orphaned; nothing to attach") and `host_epoch_stale` ("terminal belongs to an earlier gterm host incarnation"). In `_resolve_attach_locator` (~717): first statement `if row.state in {"exited", "orphaned"}: return None, _log_proxy_attach_failure(row.id, "terminal_exited")` (before `wait_startup_settled`); then `except HostEpochMismatchError` -> `host_epoch_stale` ahead of the generic `except Exception`, without `exc_info` (expected post-reboot condition). Both direct (`_handle_terminal_attach`) and proxy (`_start_proxy_attach`) paths go through this function.

Tests: `tests/servers/test_terminal_ws_attach_honesty.py::test_proxy_attach_failures_are_typed_and_finalized` gains parametrized cases `row_exited -> terminal_exited` (call `TerminalManager(temp_db).mark_exited` after `_live_row`) and `locator_epoch_stale -> host_epoch_stale` (`_LocatorRuntime(error=HostEpochMismatchError(...))`); new `test_direct_attach_refuses_exited_row`. `tests/terminals/test_native_runtime.py` (harness `_runtime()` / `FakeHostClient(host_epoch=…)` ~344): `test_attach_locator_rejects_row_from_earlier_host_epoch`, `test_attach_locator_stamps_live_epoch_when_row_matches`, `test_attach_locator_falls_back_to_row_epoch_when_host_unadopted`. `tests/storage/test_terminals.py:637` already covers the store-level mismatch. Golden fixtures need no change.

Pytest: `tests/servers/test_terminal_ws_attach_honesty.py tests/servers/test_terminal_ws_golden.py tests/servers/test_terminal_ws_lease.py tests/servers/test_native_web_proxy.py tests/terminals/test_native_runtime.py tests/terminals/test_runtime_contract.py tests/storage/test_terminals.py tests/storage/test_terminal_machine_scope.py`.

### B2. Caller liveness consults the terminal row (breaks the stuck triple)

Root cause: `_caller_session_is_live` (`src/gobby/agents/run_completion.py:359-375`) equates "session status is live" with "caller can still act". After host loss the caller's terminal row is `exited` while its session stays `active`, because the liveness monitor's query (`src/gobby/sessions/liveness_monitor.py:374-454`) excludes sessions whose run is running/pending and `expire_terminal_run_sessions` (`src/gobby/agents/agent_cleanup.py:457`) waits on the run.

Change the query to the `EXISTS … t.id = ar.terminal_id AND t.state IN ('exited','orphaned')` shape already used in `src/gobby/storage/agents/_queries.py:388` and `_termination.py:133`:

```sql
SELECT s.status,
       EXISTS (SELECT 1 FROM agent_runs ar JOIN terminals t ON t.id = ar.terminal_id
               WHERE ar.id = s.agent_run_id AND t.state IN ('exited', 'orphaned')) AS terminal_gone
FROM sessions s WHERE s.id = %s
```

Return `status in LIVE_SESSION_STATUSES and not terminal_gone`. Runs with no terminals row keep today's behavior; the unreadable-liveness `return True` path stays. Safe for in-flight reviews: both consumers (`ended_caller_close_review_outcome`, `cooperative_close_handoff_pending`) test `review.active` before liveness, so liveness only matters once the review is terminal, and a caller with an exited terminal cannot rework, cooperative-end, or receive delivery, which is exactly what SESSION_END already assumes via `caller_ended=True`. Rejected: (a) liveness-monitor expiry of running-run sessions drops a deliberate ownership rule and runs `_expire_session` side effects against a running run; (b) a "terminal missing" marker duplicates `terminals.state`. `_cleanup_missing_terminal_agent_run` and `check_completed_task_agents` need no change.

Tests: `tests/agents/test_run_completion.py` (`SimpleNamespace` reviews + `patch.object`; `temp_db` for the SQL): `test_caller_with_exited_terminal_is_not_live` (exited -> False, live -> True, no row -> True), `test_ended_caller_outcome_fires_when_caller_terminal_exited` (review `error`, session active -> `("fail", …)`), `test_active_review_owns_caller_even_when_terminal_exited` (-> None). `tests/agents/watchdog/test_close_review_parked_caller.py`: `_sweep_after_caller_terminal_exits(harness)` mirroring `_sweep_after_caller_ends` (~609) but leaving the session active and binding an exited native terminals row (`TerminalManager.promote_to_live` -> `mark_exited`); `test_terminal_loss_fails_invalid_verdict_caller` (handled == 1, caller `error`) and `test_terminal_loss_does_not_fail_active_review` (handled == 0).

Pytest: `tests/agents/test_run_completion.py tests/agents/watchdog/test_close_review_parked_caller.py tests/agents/test_lifecycle_task_completion.py tests/agents/test_lifecycle_reconciliation.py tests/test_runner_lifecycle.py`.

### B3. Spawned codex sessions get task titles, not agent-preamble titles

Root cause: codex spawns type the assembled prompt (`_agent_prompt_prefix` + task prompt, `src/gobby/agents/spawn_executor_providers.py:472-475`) into the terminal, so `handle_before_agent` (`src/gobby/hooks/event_handlers/_agent.py:148`) feeds the agent-definition preamble to `promote_heuristic_title`; and the spawn-time auto-claim in `finalize_executed_spawn` (`src/gobby/mcp_proxy/tools/spawn_agent/_execution.py:142-175`) calls storage `claim_task` directly and skips the `update_title_for_claim` the claim tool performs (`_lifecycle_claim.py:203`). Claude/Gemini spawns inject the persona as hook context, so they are unaffected.

Two one-line calls reusing existing helpers, both required (title alone leaves the junk in `heuristic_title`, which `recompute_automatic_title` restores on task close; heuristic alone gives "Backend developer Reuse delivered" instead of the task title):
1. `_execution.py` `finalize_executed_spawn`, inside `if task_owned_by_child:` after `_link_auto_claimed_session`: best-effort `await asyncio.to_thread(update_title_for_claim, runner.session_manager, spawn_result.child_session_id, claimed_task)` as a sibling helper `_title_auto_claimed_session` with the same try/except-debug shape. Reuse `gobby.sessions.title_lifecycle.update_title_for_claim` -> "project#seq: Task #NNNNN - <title>", `title_source="task"`.
2. `spawn_executor_providers.py`: `seed_heuristic_title_from_prompt(request, child_session_id)` beside `_agent_prompt_prefix`, calling `promote_heuristic_title(request.session_manager._storage, child_session_id, request.prompt or "")`; called from `_spawn_codex_terminal` (`spawn_executor.py:223`) inside the existing `if plan.inject_persona` branch via `await asyncio.to_thread(...)`. `promote_heuristic_title` persists with `COALESCE(heuristic_title, %s)` (first wins), so the later BEFORE_AGENT promotion becomes a no-op.

Tests: `tests/mcp_proxy/tools/spawn_agent/test_execution.py` (fixture ~103-160): `test_auto_claimed_task_titles_child_session` (patch `update_title_for_claim` at the `_execution` import site; called with `(runner.session_manager, child_session_id, claimed_task)` after a child-owned claim, not called when a third session owns the task). `tests/agents/test_spawn_executor.py` (next to `test_codex_agent_prompt_precedes_task_prompt` ~1061): `test_codex_spawn_seeds_heuristic_from_clean_prompt` (patch `promote_heuristic_title`; called with the clean prompt, never with the `## Agent` preamble; not called when `inject_persona` is false). `tests/sessions/test_title_lifecycle.py::test_first_heuristic_wins_and_survives_task_precedence` already pins COALESCE.

Pytest: `tests/mcp_proxy/tools/spawn_agent/test_execution.py tests/agents/test_spawn_executor.py tests/sessions/test_title_lifecycle.py tests/hooks/test_hooks_manager.py -k "title or codex or claim or heuristic"`.

### B4. Bound the code-index sync fan-out

Root cause: `_sync_pass` (`src/gobby/code_index/sync_worker.py:417`) gathers every pending file of a project (up to `sync_worker_batch_size`) with no limiter, and `_sync_vector_file_with_retry` (~95) spends its full 3-attempt budget per file even after a sibling has opened the vector breaker, so an embedding outage costs up to 150 concurrent gcode subprocesses per project per pass. No existing knob fits: `nightly_repair_concurrency` is scoped to the nightly job; `DatabaseConcurrencyConfig` sizes DB/CPU consumers.

1. `src/gobby/config/code_index.py`: `sync_worker_concurrency: int = Field(default=4, ge=1, description="Maximum concurrent per-file projection sync commands")` after `sync_worker_batch_size`.
2. `sync_worker.py` `_sync_pass`: one `asyncio.Semaphore(max(1, int(config.sync_worker_concurrency)))` per pass (pattern from `nightly_repair.py:65`), `async with semaphore:` around `sync_pending_file`'s body; keep the gather and the existing OPEN-breaker fetch gating. `_sync_vector_file_with_retry` gains `breakers: tuple[SyncCircuitBreaker | None, ...] = ()`; in the transient branch, before sleeping for the next attempt, re-raise if any breaker is `OPEN` (HALF_OPEN excluded so the probe file keeps its budget). `_sync_file` passes `breakers=(gateway_breaker, vector_breaker)`; mirror in `_sync_graph_file_with_retry` with `(gateway_breaker,)`.
3. `tests/runner_helpers.py:136`: `set_mock_default(config.code_index, "sync_worker_concurrency", 4)` beside the batch-size default.

Tests (`tests/code_index/test_sync_worker_breaker.py`, reusing `ConcurrentGraphGateway`/`max_active`, `_config(**overrides)`, `_make_storage`, `_indexed_file`, `_write_files`, `make_breaker`): `test_sync_pass_bounds_concurrency_to_config` (5 files, concurrency 2, blocking gateway -> `max_active == 2`, all synced); existing `test_sync_pass_runs_disjoint_files_concurrently` stays green; `test_vector_retry_aborts_once_breaker_opens` (transport error, `failure_threshold=1`, two files, concurrency 2, backoff `(0.0, 0.0)` -> file A 3 calls, file B exactly 1, breaker OPEN). `tests/config/test_code_index_config.py:48`: `pytest.param("sync_worker_concurrency", 0, id="zero-sync-concurrency")`.

Pytest: `tests/code_index/test_sync_worker_breaker.py tests/code_index/test_sync_worker.py tests/config/test_code_index_config.py`.

## Work stream C: unstick 13991, then the restart window (operational, no task needed)

Corrections from live state: #22602 is still open (ready, unclaimed; its review closed but the task never did). `task-22616-gclient-workspace-ui` is also dirty (12 modified gclient files, epic #22616). The main checkout is dirty with #22622's uncommitted files (`src/gobby/cli/daemon.py`, `src/gobby/cli/_daemon_handoffs.py`, `src/gobby/servers/routes/admin/_lifecycle.py`, `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`, `docs/guides/sessions.md`, tests); startup sync publishes bundled content from the main checkout, so that reference must be committed under #22622 (or stashed) before the restart.

### C1. Unstick 13991 now, while the daemon is up (not during the restart)

`gobby-agents:end_agent_run` is self-termination only and cannot target another run. Daemon reconciliation never reaches this run today (`agent_health.check_unhealthy_agents` skips runs whose `terminal_for()` is None; `list_termination_candidates` requires a live terminal and an expired session). Use:

1. `gobby-agents:stop_agent(run_id="c7d74616-54b4-47f3-b8bb-a1bbed9a380f")`. Path: `agent_cancellation.stop_agent_run` -> `kill_agent` (`src/gobby/agents/kill.py`), which returns `already_dead / terminal_exited` without touching gterm (no epoch handshake) -> run `cancelled` with `terminal_reason=user_cancelled` -> session d1a0f591 set `expired` (`agents_termination._cleanup_terminal_artifacts`) -> `task_recovery.recover_task_from_terminal_agent(outcome="cancelled")` releases the #22561 claim. #22561 has `allow_automation=false`, so nothing respawns. Do not use `kill_agent(status="error")`: that bumps `dispatch_failure_count` and fails the stage.
2. Verify read-only: `gobby-tasks:get_task("#22561")` unclaimed and ready; `gobby-sessions:get_session` for gobby#13991 expired; no running agent runs. Leave close review `61d2cd87` as `error` (terminal; a fresh `close_task` creates a new review).
3. #22405: leave alone. Escalated tasks are excluded from claim sweeps by design and `allow_automation=false`; when the cohort decision is made, `claim_task("#22405", force=True)` then `de_escalate_task`. #22567: normal claim/fix/close path, owned by the worktree-recovery agent.

Never mutate task, session, or run rows via SQL, the `gobby tasks` CLI, or REST.

### C2. Restart window (after streams A and B are committed)

Preconditions, all four, polled no faster than every 60 s:

```
! pgrep -f 'ops/daily-chain.sh' >/dev/null \
&& ! pgrep -f 'run-job.sh --name replenishment-daily' >/dev/null \
&& ! pgrep -f 'call-tool lightspeed' >/dev/null \
&& ! test -e /tmp/game-goblins-lightspeed-sync.lock
```

`gobby restart --wait` does not cover this external cron (it only waits on Gobby-protected cron runs and unresolved handoffs). Also require no live spawned worker and no close validator (`gobby-agents:list_running_agents`), and commit or stash the #22622 files in the main checkout.

Sequence:
1. `gobby-agents:send_message(target="global")`, no wake: "Daemon restart in ~2 minutes from /Users/josh/Projects/gobby to load the post-reboot liveness and attach fixes (#tasks). gterm host and all native terminals survive; gclient reconnects on its own. MCP calls fail for ~20-30 s. Do not start close validators or spawn agents until a 'restart complete' message follows."
2. `cd /Users/josh/Projects/gobby && uv run gobby restart` (main checkout only). No `--terminals` (drains the gterm host and kills every live pane on the machine). No `--force`. The preflight (`src/gobby/cli/daemon_preflight.py restart_start_refusal`) proves the start half first and leaves pid 917 alone on refusal.
3. Post-checks: `uv run gobby status` shows a new pid, the binary set not mixed, gterm adopted with the same host pid 1426 and epoch `32bd40db…`; `tail -60 ~/.gobby/logs/errors.log` has no new `HostEpochChangedError`, `PostgreSQL hub pool acquisition failed`, or `DatabaseExecutor is shut down`; clicking gobby#13991 in gclient yields a clean exited refusal, not `proxy_start_failed`; run c7d74616 is still `cancelled`.
4. Send the "restart complete" global message.

gterm stays out of this window: a rebuilt gterm goes live only via `gobby stop --terminals` then `gobby start`, which kills every live terminal (memory 9ca82b73). None of the fixes need it. gclient is promoted separately and goes live when the user relaunches gclient (pid 1811).

### C3. Load rule for this coordinator

Peak last night was 17 overlapping runs, about half of them close validators spawned by workers closing at once; the hub pool was the choke point. This plan runs one implementation worker at a time and lets its close validator finish before the next spawn. No agent-run cap exists today (hence the `needs-decision` task); the only related knobs are `cron.max_concurrent_jobs` (5), `code_index.symbol_summary.max_concurrency` (2), `ai.spawn_cold_max_concurrency` (3), and `postgres_pool.max_size` (2, restart-required).

## Coordinator runbook: tasks, spawns, landing, order

### Mechanics (verified in source)

- **Spawn.** `gobby-agents:spawn_agent(agent="backend-developer", task_id=…, provider="codex", model="gpt-5.6-sol", reasoning_effort="xhigh", prompt=…)`. `model` requires explicit `provider`; the explicit model bypasses the feature-profile substitution (memory 8743df40). `backend-developer.yaml` already defaults to exactly this provider/model/effort. Spawn auto-claims an unclaimed task for the child and pre-advances the workflow's claim step (`src/gobby/mcp_proxy/tools/spawn_agent/_execution.py:195-260`). A `pending|running` run on the same task makes spawn return `skipped: true` with the old run's metadata (`_spawn_guards.py:207-238`).
- **Worktree reuse refuses dirty trees.** Both `worktree_id=` and `isolation="worktree"`+`task_id` reuse run `_ensure_clean_worktree` (`src/gobby/agents/worktree_reuse.py:186-204`): staged, unstaged, and untracked source files all refuse with "Cannot reuse worktree with uncommitted changes". Clean reuse rebases the branch onto its base (120 s, conflict -> `reused_worktree_rebase_conflict`). So an interrupted worker with uncommitted work is resumed with `isolation="none", project_path="<its worktree>"` (`src/gobby/agents/isolation_none.py:16-21`; legal because each worktree has its own `.gobby/project.json`). No rebase happens; the later merge handles drift.
- **Main-checkout work.** `isolation="none", project_path="/Users/josh/Projects/gobby"`. No dirty-checkout guard and no cross-session lock exist, so such a worker runs strictly alone, with no `merge_worktree` in flight (the merge checks out `0.5.0` in that same checkout and refuses on overlapping dirty paths, `src/gobby/mcp_proxy/tools/worktrees/_sync.py:466-502`). Commits there land on `0.5.0` directly.
- **Landing.** `gobby-worktrees:merge_worktree(worktree_id=…)` (defaults: source = the row's branch, target = the row's `base_branch`; `--no-ff` merge; no validation; marks the row `merged`; does not delete). Task worktrees 22593/22602/22621 have base `lane/22581-gcode-import-communities`, the rest `0.5.0`. Conflicts: `gobby-merge:merge_start` -> `merge_status` -> `merge_resolve` (one conflict at a time) -> `merge_apply` -> one `merge_worktree`. Delete later with `gobby-worktrees:delete_worktree(worktree_id, merged_into=…)` only after the user confirms.
- **Waiting.** `gobby-agents:wait_for_agent(run_id)` yields: `completed: false, notification_registered: true` means end the turn; on wake, call `wait_for_agent(run_id)` again first. The worker's own `close_task` spawns the close validator as the worker's child and the worker absorbs that wait; the coordinator waits only on its own run id. Failure arrives as the same payload with `status` error/cancelled and `terminal_reason`.
- **Worker prompt, every spawn:** the task ref and what is already done (commits, dirty paths); "your cwd is …"; "commit by explicit path only, never `git add -A`; do not touch other sessions' files; do not spawn implementation workers; do not restart or cut over the daemon; validate per AGENTS.md, then `close_task(preview=true, commit_sha=…)`, repair blockers, then `close_task`". For the fix tasks the task description carries the design section verbatim from this plan.

### Tasks to create (`gobby-tasks:create_task`)

- Leaf: "gclient: close terminal and control items act on the menu row's pane, not the focused pane" (stream A; description = stream A text; `isolation` worktree).
- Epic: "Post-reboot daemon recovery: dead-epoch attach refusal, terminal-aware caller liveness, spawned-session titles, bounded sync fan-out", four leaves B1-B4 (descriptions = the B sections). The restart and its post-checks are the epic's close evidence.
- Task, `needs-decision`, not started: "spawn_agent: configurable host-wide cap on concurrent agent runs in `_spawn_guards.py`" (last night's 11-18 concurrent runs plus per-worktree cargo rebuilds saturated the machine; decide default and scope).

### Serial queue (one implementation worker at a time; each step is spawn -> wait -> land)

Current facts: every listed task is `ready` and unclaimed; only #22561 has a running run (c7d74616). Worktree rows: 22560 (1 commit, clean, task closed), 22561 (1 commit, clean), 22564 (1 commit, clean), 22567 (1 commit + 3 dirty), 22621 (1 commit, clean, lane), 22602 (1 commit + 2 dirty, lane), 22562 (0 commits, 11 staged), 22563 (0 commits, 9 staged), 22565 (0 commits, 8 unstaged + 1 untracked yaml), 22593 (0 commits, 3 dirty, lane), task-22616 worktree (14 dirty gclient files, #22617), lane/22581 (2 commits ahead of 0.5.0). #22622 has no worktree row; its work sits in the main checkout alongside other sessions' dirty entries that must be left alone (`docs/research/gcode-evidence-cohort-sonnet.md` and the cohort answer-key removal for #22405, the deleted `.gobby/plans/*` files and untracked `.gobby/plans/completed/gclient-direct-input.md`, staged `tests/e2e/test_terminal_client_stack.py`).

1. **C1 unstick.** `stop_agent(run_id="c7d74616-…")`; verify #22561 unclaimed, 13991 expired (see stream C).
2. **#22622** in the main checkout (`isolation="none", project_path="/Users/josh/Projects/gobby"`). Prompt names exactly its seven paths (`src/gobby/cli/daemon.py`, `src/gobby/cli/_daemon_handoffs.py`, `src/gobby/servers/routes/admin/_lifecycle.py`, `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`, `docs/guides/sessions.md`, `tests/cli/test_daemon_handoffs.py`, `tests/servers/routes/test_admin.py`), the last recorded run (66 passed, 2 failed), and the other dirty entries to leave untouched. First so the main checkout is clean of #22622 before any merge, and so the #22567 guard cannot block the restart.
3. **Land #22560**: `merge_worktree` only (task already closed). Then **#22564** (clean, 1 commit; spawn with `isolation="worktree"`, prompt: close review was interrupted, finish closure) -> land. Then **#22561** (same shape; prompt: run was reconciled, re-claim happens on spawn, finish closure) -> land. Then **#22621** (lane base; spawn `isolation="worktree"`, base stays `lane/22581-…`) -> `merge_worktree` into the lane.
4. **#22566** and **#22623**, closure only, commits already on `0.5.0` (`dcb8032e8d`, `7624b06efa`): one worker each in the main checkout (`isolation="none"`), prompt: validate and `close_task(commit_sha=…)`; no merge step.
5. **#22567** (`isolation="none", project_path=<task-22567 worktree>`; 1 commit + the three reviewer-fix files; the invalid verdict concerns criterion 2 wording) -> land. Must land before the restart.
6. **Stream A** task -> spawn `isolation="worktree"` -> land -> coordinator runs `uv run gobby install --no-interactive` and checks `uv run gobby status` (gclient not stale). User relaunches gclient when convenient.
7. **Stream B** leaves B2, B1, B4, B3: spawn `isolation="worktree"` each -> land each. Pre-restart gate after B3 lands: `uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/ && uv run mypy src/` in the main checkout.
8. **C2 restart** (stream C; the cron precondition is already met): no worker or validator live, announce, `uv run gobby restart`, post-checks, "restart complete", close the epic with that evidence.
9. **Remaining interrupted work**, each `isolation="none"` in its own worktree, then land: **#22562** (11 staged, review/commit/close), **#22563** (9 staged), **#22565** (8 unstaged + untracked rule yaml; test-quality repairs remain), **#22602** (lane; the two-file SQL binding-order correction) -> lane, **#22593** (lane; `remap.rs` untracked; `tdd:required`) -> lane, then **#22617** in the task-22616 worktree (rebases onto the landed stream A change). Finally land `lane/22581-…` onto `0.5.0` via its worktree row.
10. Left with the user: #22405 (escalated, cohort decision), #22557 and the 13977 bookkeeping, #22620 (Claude session). gterm rebuild in a separately announced drained window.

Refusals to expect and handle: `skipped: true` on #22561 if C1 was not done; `reused_worktree_rebase_conflict` on steps 3/6/7 (resolve with the merge tools, never a fresh branch, memory f1e1200c); "Target checkout has uncommitted changes that overlap merge" on landing (finish or stash the overlapping session's files, never discard).

## Source of the recovery order: `docs/plans/morning-recovery.md` (codex audit, session gobby#14019)

Used for its audit and dependency order only; its `codex resume` commands are not used. Facts carried into the queue: #22564 closes and lands before other worktree merges; #22621 lands before #22593 resumes (its TDD gate was blocked by that defect); #22567 is the guard against startup sync publishing dirty templates and lands before any restart; #22622's files do not overlap stream B's files, so it can go first; #22560 is closed but unmerged; #22566 is merged but open with a stale invalid verdict; #22596 is closed and merged into the communities lane, whose landing onto `0.5.0` is still pending; the task-22616 worktree holds #22617's 14 modified gclient files, so stream A lands first and #22617 rebases onto it.

## Verification

Per leaf, in the session transcript: `uv run ruff format <files>`, `uv run ruff check src/ <test dirs>`, `uv run mypy src/`, and the leaf's targeted pytest with `GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`. Stream A: `cargo fmt -p gobby-client`, `cargo clippy -p gobby-client --all-targets`, `cargo nextest run -p gobby-client`, then the install and `gobby status` check. Never run the full pytest suite.

Before the restart: `uv run ruff format --check src/ tests/ && uv run ruff check src/ tests/ && uv run mypy src/`, plus the four B pytest sets.

End to end, after the restart:
- `uv run gobby status`: new daemon pid, binary set not mixed, gterm adopted with host pid 1426 and epoch `32bd40db…`, gclient not stale.
- `tail -60 ~/.gobby/logs/errors.log`: no `HostEpochChangedError`, no `PostgreSQL hub pool acquisition failed`, no `DatabaseExecutor is shut down`.
- In gclient (new binary): open the context menu on an agent row whose pane is not focused and choose "close terminal": only that terminal's `terminal_kill` is sent and the focused pane survives. Click gobby#13991: the attach is refused with `terminal_exited`, no retry loop, nothing new in errors.log.
- Read-only DB check: `SELECT status, terminal_reason FROM agent_runs WHERE id='c7d74616-…'` is `cancelled`; `SELECT claimed_by_session_id FROM tasks WHERE seq_num=22561` is NULL; `SELECT count(*) FROM agent_runs WHERE status='running'` is 0 unless new work was started.
- Spawn one codex agent on a claimed task (only after the recovery batch is done): its session title reads "Task #NNNNN - <title>", not the agent preamble.
- `grep -c "transient vector sync failure" ~/.gobby/logs/daemon.log` over the next hour stays flat while LM Studio is healthy; if the endpoint is stopped deliberately, the breaker opens after one file and siblings stop retrying.
