Plan artifact: `.gobby/plans/native-runtime-completion.md`

# Native Runtime Completion

**Plan ID:** native-runtime-completion

## Overview
`kind: framing`

`herdr-client-completion` deferred this work as its D1 section (task_ref #21357,
original acceptance item 5.1.2): "Native-runtime hardening and the default flip are
daemon/host work with their own blast radius; the client epic consumes today's opt-in
native path and must not carry a second subsystem rework." `ROADMAP.md`'s Stage 0
paragraph under "The path" names this epic as tail work that is not dispatchable
before the client epic lands, and records the standing default: "tmux remains the
default; native PTY launches are opt-in."

This plan re-specifies the QA plan's sections 2.3–2.9, 3.1–3.4, 4.1, 4.2, 4.6, 5.1, 5.2,
8.1, 8.2 and the flip (`.gobby/plans/herdr-terminal-client-qa-fixes.md`, source
material only) from the landed tree. That QA plan was written against a failed
worktree build and was never expanded (#21334 came from `herdr-client-completion`);
symbols and tests it names that are absent from `0.5.0` are not owed by this plan, and
the landed tree is the only authority for what exists. Landed and therefore not
re-specified: #21202's
stale-pending reaper shell; #22002's host survival across daemon restarts with
`handle_host_death` never respawning; the client epic's opt-in native path; the single
attachment-scoped `terminal_input` handler; the host's `host_draining` refusals for
`spawn` and `attach`; `with_id` request-id echo; the removed `license-file`; the
`daemon_resume_keys` constants; and the retired 8.2 placebo tests.

Out of scope: gclient (client epic), tmux runtime behaviour changes, Stage 2 route
families, and the flip itself (D1).

## Constraints
`kind: framing`

**Decision Record (confirmed 2026-09-09).**

1. Sole operator. The flip gate is the least mechanism that is still honest: P7's
   host-driven acceptance suite green on macOS and Linux in ordinary CI, recorded by
   hand in `docs/evidence/native-backend-flip.md`, checked by one local test. No weekly
   producer workflow and no remote provenance verifier.
2. Respawn policy. A host that dies unexpectedly mid-run is respawned by a singleflight
   `ensure_restart()` with 1 s to 30 s doubling backoff, reset after 60 s healthy, giving
   up after 5 consecutive failures. Drain (`stop(drain_host=True)`) and daemon restart
   never respawn; #22002 stays.
3. Named defaults: control line cap 2 MiB (`control_overflow`); delta lag close 5 s;
   control queue deadline 2 s; commit deadline 30 000 ms (minimum 1 000); parity PTY
   120×40; web history 500 lines pinned, 2 000 lines or 256 KiB maximum; fragment
   overhead charge 256 B.
4. Web verification is Playwright at 440×956, 932×430, and 1440×900; no live URL gate.
5. Guard set G (`docs/guides/gterminal-development-guide.md` § "Guard set G") is the
   close set for every leaf, run from the `0.5.0` checkout with the isolated test hub
   DSN and `GOBBY_TEST_PROTECT=1`; its historical carve-outs have all expired and none
   applies here. Each deliverable's last acceptance item names the groups it must run,
   so the gate reaches every leaf's validation criteria. Group 7 (host leak) is
   automated by 3.1's session-scoped leak check.
6. Monolith ceiling. Files at or above 850 lines that this plan touches, with current
   counts: `crates/gterminal/src/host/state.rs` 914, `src/gobby/runner_init/orchestration.py`
   910, `src/gobby/agents/lifecycle_monitor.py` 996, `src/gobby/servers/websocket/terminal_ws.py`
   847 (treated as at-ceiling), `web/src/hooks/useTmuxSessions.ts` 880. Every deliverable
   that targets one of them names the split file it moves code into.
7. No backward compatibility: wire changes regenerate goldens from
   `crates/gterminal/tests/wire_golden.rs`; the fake control `attach` verb is deleted,
   never shimmed.
8. Rust edits load the `rust` skill; crate changes are live only after rebuild and a
   new-inode install (`cargo build --release -p gobby-terminal --features vt-engine --bin gterm`).
9. Any daemon restart is announced to the epic #21805 coordinator first.
10. Consumer sweeps for removed fields were run on the planned branch:
    `gcode grep -w tmux_session_name src/gobby/agents/resume_executor.py src/gobby/runner_lifecycle_processes.py src/gobby/agents/spawners/base.py`
    hits `resume_executor.py:503,737`, `spawners/base.py:22`, `runner_lifecycle_processes.py:99`;
    `gcode grep -F "_frame_host_epoch" src` hits `runner_lifecycle_subsystems.py:288-289`
    and `native_runtime.py:128,189,201`; `gcode grep -F "WriteCoordinator(" src` hits
    `lifecycle_monitor.py:128` and `runner_init/orchestration.py:564`.
11. Unchanged consumers are still Targets. Every same-repository consumer of an exact
    symbol Target is listed in that deliverable, as expansion validation requires, with a
    scope-reason saying whether an edit is expected. `ServiceContainer` is targeted at
    file scope (`app_context.py::*`) because it only gains two attributes and its readers
    never change. The re-verified consumers that expect no edit are `WriteRequest` and
    `WriteCoordinator.write` callers (an optional `idempotency_key`), `_runtime_spawn`
    and `init_orchestration` fixture users (unchanged signatures), `AgentLifecycleMonitor`
    constructors (unchanged parameters), `HostConfig::default` in `host/write.rs`
    (new fields carry defaults), and `TerminalHostManager` constructors in `runner.py`
    (new config fields carry defaults); their leaves re-verify the call sites.

## P1: Pending-row lifecycle and typed spawn failures
`kind: framing`

**Goal**: every spawn outcome is typed, every pending row settles exactly once, the
reaper terminates by backend identity, and no tmux alias survives on `SpawnResult`.

### 1.1 Remove `SpawnResult` tmux aliases and route liveness and cleanup through the runtime [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_models.py::SpawnResult`
- `src/gobby/agents/spawners/base.py::SpawnResult`
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::_check_tmux_session_alive`
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::_deferred_tmux_health_check`
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::_start_tmux_health_check`
- `src/gobby/mcp_proxy/tools/spawn_agent/_health.py::schedule_tmux_health_check`
- `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py::_terminate_spawn_process`
- `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py::_capture_then_kill_spawn_session`
- `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py::cleanup_failed_spawn`
- `src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py::_tmux_runtime_metadata`
- `src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py::_build_spawn_success_response`
- `src/gobby/mcp_proxy/tools/spawn_agent/_response.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_execution.py::*` — scope-reason: the only caller of the health scheduler, cleanup, and response builders follows their new names and module
- `src/gobby/agents/tmux/spawner.py::*` — scope-reason: the tmux spawner's `SpawnResult` construction drops the alias keywords
- `tests/mcp_proxy/tools/spawn_agent/test_health.py::*` — scope-reason: health cases assert runtime liveness
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py::*` — scope-reason: execution cases import the response builder from `_response.py`
- `src/gobby/agents/resume_executor.py::*` — scope-reason: both `tmux_session_name` reads (lines 503 and 737) become terminal-row reads and the resume path resolves the runtime per row (memory 168b7ae0)
- `src/gobby/runner_lifecycle_processes.py::*` — scope-reason: the last `tmux_session_name` consumer outside `terminal_context` reads the terminal row instead
- `tests/terminals/test_no_direct_tmux_consumers.py::*` — scope-reason: `_FIELD_SWEEP_ALLOWED` shrinks to the empty set
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::*` — scope-reason: cleanup assertions follow `runtime.terminate`
- `tests/mcp_proxy/tools/spawn_agent/test_response.py`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: alias-field assertions are rewritten against `terminal_id`
- `tests/agents/test_spawn_executor_providers.py::*` — scope-reason: provider `SpawnResult` constructions drop the alias keywords
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: the mocked `SpawnResult` at line 1016 drops the alias keywords
- `src/gobby/agents/spawn_executor_providers.py::*` — scope-reason: both `SpawnResult(` constructions (lines 156 and 204) drop the alias keywords
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: the three `SpawnResult(` constructions (lines 183, 209, 232) drop the alias keywords
- `src/gobby/agents/spawn_executor.py::_promote_prepared`
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py::*` — scope-reason: migrate the removed response-builder import and all three response tests to _response.build_spawn_response
- `src/gobby/agents/spawners/__init__.py::*` — scope-reason: re-exports `SpawnResult`; the export list is re-verified after the alias removal
- `tests/integration/test_terminal_mode_worktrees.py::*` — scope-reason: `SpawnResult` constructions and assertions drop the alias fields
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: the `cleanup_failed_spawn` call site follows the runtime-based cleanup
- `tests/mcp_proxy/tools/spawn_agent/test_durable_spawn_error.py::*` — scope-reason: `cleanup_failed_spawn` assertions follow `runtime.terminate`
- `tests/workflows/test_step_snapshot_semantics.py::*` — scope-reason: the `cleanup_failed_spawn` patch site follows the runtime-based cleanup

Both `SpawnResult` dataclasses lose `tmux_session_name`, `tmux_pane`, and every alias
property; the only identity is `terminal_id` plus `backend`. `_check_tmux_session_alive`
becomes `_terminal_is_live(row)` that resolves the runtime from the registry by
`row.backend` and calls `runtime.is_live(row)`; nothing gates on `tmux_pane` (memory
5c7244d8). `_terminate_spawn_process` and `_capture_then_kill_spawn_session` call
`runtime.terminate(row)` and then `TerminalManager.fail_pending` for pending rows or
`mark_exited` for live rows; they keep the existing pid start-time identity check before
any SIGKILL. `_build_spawn_success_response` moves to the new `_response.py` and is
renamed `build_spawn_response` (the one name used by `_execution.py`'s import, the
tests, and the acceptance below); `_tmux_runtime_metadata` moves with it. `_response.py`
owns the MCP response dict (`terminal_id`, `backend`, `agent_run_id`, `error`,
`error_detail`). `resume_executor.py` writes
`SPAWN_KEY_KEY` from the terminal row's `spawn_key`. `_FIELD_SWEEP_ALLOWED` becomes
empty; the `terminal_context`/`tmux_context` carve-outs already coded in the sweep
stay.

**Acceptance:**

- 1.1.1 - Neither `SpawnResult` has a `tmux_session_name` or `tmux_pane` attribute and constructing either with those keywords raises `TypeError`. symbol: `SpawnResult`. test: `tests/agents/test_spawn_executor.py::test_spawn_result_has_no_tmux_aliases`.
- 1.1.2 - Spawn-agent health checks a native row through `NativeTerminalRuntime.is_live` and a tmux row through `TmuxTerminalRuntime.is_live`, never by pane. test: `tests/mcp_proxy/tools/spawn_agent/test_response.py::test_health_uses_runtime_liveness_per_backend`.
- 1.1.3 - Failure cleanup terminates through the row's runtime and settles the row (`fail_pending` for pending, `mark_exited` for live); no `tmux kill-session` is invoked from `_failure_cleanup.py`, and SIGKILL still requires a matching pid start time. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_terminates_via_runtime_and_settles_row`.
- 1.1.4 - `_FIELD_SWEEP_ALLOWED` contains no `src/gobby/` path and the sweep passes. test: `tests/terminals/test_no_direct_tmux_consumers.py::test_tmux_session_name_field_sweep`.
- 1.1.5 - Resume writes `SPAWN_KEY_KEY` from the terminal row and the MCP success response is built by `build_spawn_response` in `_response.py`; no `_build_spawn_success_response` symbol remains. symbol: `build_spawn_response`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_response.py`.
- 1.1.6 - Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 1.2 Typed native spawn failure outcomes and singleflight host respawn [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/terminals/host_client.py::HostClient._roundtrip`
- `src/gobby/terminals/host_client.py::HostClient.spawn_commit`
- `src/gobby/terminals/host_client.py::HostCommandError`
- `src/gobby/terminals/host_client.py::HostClient.raise_for_payload`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.commit_spawn`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.prepare_spawn`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.reserve_observer`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.__init__`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.handle_host_death`
- `src/gobby/terminals/host_manager.py::TerminalHostManager._health_loop`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.stop`
- `src/gobby/agents/spawn_models.py::SpawnResult`
- `src/gobby/agents/spawn_executor.py::_runtime_spawn`
- `src/gobby/agents/spawn_executor.py::_promote_prepared`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_create`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_kill`
- `src/gobby/servers/websocket/terminal_ws_create.py`
- `src/gobby/servers/websocket/server.py::*` — scope-reason: the dispatch table binds `_handle_terminal_create` and `_handle_terminal_kill` from the new mixin
- `src/gobby/config/terminal_host.py::TerminalHostConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated checked-in contract for the two new host restart fields
- `src/gobby/servers/websocket/tmux.py::TmuxMixin._handle_terminal_kill`
- `tests/terminals/test_host_client.py`
- `tests/terminals/test_native_runtime.py::*` — scope-reason: `commit_spawn`, `prepare_spawn`, and `reserve_observer` cases assert typed outcomes
- `tests/terminals/test_runtime_contract.py::*` — scope-reason: the cross-backend contract suite pins `SpawnResult.error` codes
- `tests/terminals/test_backend_selection.py::*` — scope-reason: backend-selection fakes return typed failures
- `tests/e2e/test_terminal_client_stack.py::*` — scope-reason: the e2e stack asserts `spawn_commit`'s typed transport error
- `tests/config/test_terminal_host.py::*` — scope-reason: the two new fields join the host config cases
- `tests/config/test_terminal_host_config.py::*` — scope-reason: the two new fields join the host config cases
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: create and kill goldens bind the new mixin
- `tests/terminals/test_host_manager.py::*` — scope-reason: respawn backoff, singleflight, and give-up cases join the existing start, adopt, and death suites
- `tests/agents/test_native_spawn.py::*` — scope-reason: every failure branch asserts its typed outcome
- `tests/servers/test_terminal_ws_create.py::*` — scope-reason: `terminal_create_result.code` assertions
- `tests/config/test_terminals.py`
- `tests/config/test_runtime_config_contract.py::*` — scope-reason: the checked-in contract gains the two host restart fields
- `src/gobby/runner.py::*` — scope-reason: `TerminalHostManager` construction and `stop` call sites re-verified against the two new restart config fields; no change expected
- `src/gobby/config/app.py::*` — scope-reason: `TerminalHostConfig` composition re-verified; the new fields carry defaults
- `tests/test_runner_lifecycle_processes.py::*` — scope-reason: host-manager construction and stop fixtures re-verified against the new config fields
- `tests/agents/test_srt_spawn.py::*` — scope-reason: `_runtime_spawn` fixtures assert the typed `SpawnResult.error` outcomes
- `tests/mcp_proxy/tools/spawn_agent/test_agy_gate.py::*` — scope-reason: `_runtime_spawn` fixtures follow the typed outcome contract
- `tests/servers/test_terminal_ws_kill.py::*` — scope-reason: `_handle_terminal_kill` cases follow the bounded `code` and `reason` fields

Outcome table, with `classify_native_spawn_failure(exc) -> tuple[code, detail, settlement]`
in `native_runtime.py` as the single source:

| Failure | `code` | Row settlement |
|---|---|---|
| reserve refused (`host_draining`, `capacity`, `stale`, `not_native`) | `host_refused:<reason>` | `fail_pending` |
| prepare transport error before write | `host_unreachable` | `fail_pending` |
| host epoch changed between reserve and commit | `host_epoch_changed` | `fail_pending`; never promoted |
| commit transport error, request not written | `commit_not_sent` | `fail_pending`, host row killed by id |
| commit transport error after the request is written (drain, read, or deadline failure) | `commit_indeterminate` | stays pending; 3.2 settles from `list` |
| caller cancelled before the commit request is written | `commit_not_sent` | `fail_pending`, host row killed by id; `CancelledError` re-raised, no `SpawnResult` |
| caller cancelled after the commit request is written | `commit_indeterminate` | stays pending; `CancelledError` re-raised, no `SpawnResult`; 3.2 settles from `list` |
| commit refused (`unknown_terminal`, `already_committed`) | `commit_refused:<reason>` | `fail_pending` |
| exec failure from the gate (2.2) | `exec_failed:<code>` | `fail_pending`, detail carries the errno text |
| commit deadline expired inside the host (2.2 `exec_timeout`) | `exec_timeout` | `fail_pending`; the host has already killed the child and removed the slot |
| unparseable exec status from the gate (2.2 `malformed_status`) | `malformed_status` | `fail_pending`; the host has already killed the child and removed the slot |
| host stopped or manager stopped | `host_stopped` | `fail_pending` |

`HostClient.raise_for_payload` raises `HostCommandError(error, code=, detail=, stage=)`
with the host reply's `code`, `detail`, and `stage` fields preserved (today it keeps only
the error string), and `classify_native_spawn_failure` reads them, so `exec_failed:<code>`
carries the errno name and `error_detail` carries the host's `detail` and `stage`
through both the agent `SpawnResult` and `terminal_create_result`.
`CommitTransportError(request_written: bool)` is raised by `HostClient._roundtrip` only
for the `spawn_commit` verb; `request_written` is `False` for any failure before
`writer.write()` returns and `True` for every failure after it (drain error, connection
loss during the read, deadline). Cancellation follows the same boundary: the
`spawn_commit` coroutine records whether the write completed and its `finally` block
settles the row from that flag before `CancelledError` propagates. Other verbs keep
`HostUnavailableError`. `SpawnResult`
gains `error: str | None` (a code of at most 128 characters) and
`error_detail: str | None`; `terminal_create_result` carries `code` (at most 128
characters) and `reason` (detail). Move `_handle_terminal_create` and
`_handle_terminal_kill` out of `terminal_ws.py` into a `TerminalCreateMixin` in the new
`terminal_ws_create.py` (the `terminal_sizing.py` pattern) so `terminal_ws.py` shrinks
below 780 lines, and bind them in `server.py`'s dispatch table from the new mixin.

Respawn: `handle_host_death` calls `ensure_restart()` unless `_stop_requested` or
`host_drained` is set. `ensure_restart` is singleflight on `_restart_task` and
`_restart_generation` under `_restart_lock`; it sleeps `backoff_seconds` (1 s, doubling
to `restart_backoff_ceiling_seconds`), `_health_loop` resets the backoff to 0 after
60 s of healthy pings, and after `restart_max_attempts` consecutive failures it sets
`native_available=False` and raises `HostManagerStopped` to waiting callers.
`TerminalHostConfig.restart_max_attempts` (default 5, minimum 1) and
`restart_backoff_ceiling_seconds` (default 30.0, greater than 0) are the only new
fields; regenerate `runtime_config_contract.json` for them.

Every caller awaits `asyncio.shield(_restart_task)`, so a cancelled waiter never
cancels the shared restart. `stop` sets `_stop_requested` under `_restart_lock` before
cancelling the task; `ensure_restart` raises `HostManagerStopped` while stopping,
stopped, or drained and mints no task; the restart publishes clients and epoch and
clears its task slot only while its task identity and `_restart_generation` are current
under the same lock. Only `start` clears the stop fence.

**Acceptance:**

- 1.2.1 - Each row of the outcome table yields the named `SpawnResult.error` code and row settlement; `commit_indeterminate` leaves the row pending. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table`.
- 1.2.2 - `HostClient._roundtrip` raises `CommitTransportError(request_written=False)` when the write fails and `request_written=True` when the read fails after a complete write; other verbs raise `HostUnavailableError`. test: `tests/terminals/test_host_client.py::test_commit_transport_error_reports_written_state`.
- 1.2.3 - After an unexpected host death `ensure_restart` respawns once for concurrent callers, backs off 1, 2, 4 … 30 s, resets after 60 s healthy, and after five failures raises `HostManagerStopped` and sets `native_available=False`; a drained or stopping manager never respawns. test: `tests/terminals/test_host_manager.py::test_ensure_restart_is_singleflight_with_backoff`, `tests/terminals/test_host_manager.py::test_drained_host_is_never_respawned`.
- 1.2.4 - `terminal_create_result` carries `code` of at most 128 characters and `reason` detail for every failure branch, served from `TerminalCreateMixin`. test: `tests/servers/test_terminal_ws_create.py::test_create_result_code_is_bounded`.
- 1.2.5 - `TerminalHostConfig` validates `restart_max_attempts >= 1` and `restart_backoff_ceiling_seconds > 0`, and the checked-in runtime config contract carries both fields. test: `tests/config/test_terminals.py::test_host_restart_fields_validate`, `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.
- 1.2.6 - Two waiters share one restart during backoff; cancelling one raises `CancelledError` in that waiter only, the survivor receives the new epoch, and exactly one host spawn occurs. test: `tests/terminals/test_host_manager.py::test_waiter_cancellation_does_not_cancel_shared_restart`.
- 1.2.7 - `stop` during backoff prevents late client publication, every caller arriving during or after teardown receives `HostManagerStopped` without a spawn or token rotation, and a later explicit `start` permits one fresh restart; the test drives a fake monotonic clock and controlled sleep futures with no wall-clock wait. test: `tests/terminals/test_host_manager.py::test_stop_fences_restart_creation_and_publication`.
- 1.2.8 - Outcome row `reserve refused`: for each of `host_draining`, `capacity`, `stale`, and `not_native`, `SpawnResult.error` is `host_refused:<reason>` with the host's exact reason and the row is failed through `fail_pending`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[reserve_refused]`.
- 1.2.15 - Outcome row `host epoch changed`: `host_epoch_changed`, `fail_pending`, and the row is never promoted even when the new epoch lists a terminal with the old id. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[host_epoch_changed]`.
- 1.2.16 - Outcome row `cancelled before write`: against a real `HostClient` over a paused socket, a spawn task cancelled while blocked before `writer.write()` raises `CancelledError` to its caller, returns no `SpawnResult`, drops its correlation entry, kills the host row by id, and leaves the row failed `commit_not_sent`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[cancelled_before_write]`.
- 1.2.17 - Outcome row `cancelled after write`: a spawn task cancelled after `writer.write()` of the commit request raises `CancelledError` to its caller, returns no `SpawnResult`, and leaves the row pending `commit_indeterminate` for the 3.2 list cut. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[cancelled_after_write]`.
- 1.2.18 - Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.
- 1.2.9 - Outcome row `prepare transport error before write`: `host_unreachable` and `fail_pending`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[prepare_unreachable]`.
- 1.2.10 - Outcome row `commit transport error, request not written`: `commit_not_sent`, `fail_pending`, and the host row is killed by its id. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[commit_not_sent]`.
- 1.2.11 - Outcome row `commit transport error, request written`: `commit_indeterminate` and the row stays pending for list settlement. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[commit_indeterminate]`.
- 1.2.12 - Outcome row `commit refused`: `commit_refused:<reason>` carries the host's reason and the row is failed through `fail_pending`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[commit_refused]`.
- 1.2.13 - Outcome row `exec failure from the gate`: `exec_failed:<code>` with the errno text in `error_detail` and `fail_pending`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[exec_failed]`.
- 1.2.14 - Outcome row `host stopped or manager stopped`: `host_stopped` and `fail_pending`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[host_stopped]`.
- 1.2.19 - Outcome row `commit deadline expired inside the host`: a host reply of `exec_timeout` yields `SpawnResult.error="exec_timeout"` and `fail_pending` with no daemon-side kill, because the host has already removed the slot. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[exec_timeout]`.
- 1.2.20 - Outcome row `unparseable exec status from the gate`: a host reply of `malformed_status` yields `SpawnResult.error="malformed_status"` and `fail_pending` with no daemon-side kill. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[malformed_status]`.
- 1.2.21 - `raise_for_payload` on a reply carrying `code`, `detail`, and `stage` raises `HostCommandError` exposing all three, and an `exec_failed` reply reaches the agent `SpawnResult` as `error="exec_failed:ENOENT"` with `error_detail` naming the strerror text and stage, and reaches `terminal_create_result` as `code="exec_failed:ENOENT"` with the same `reason`. test: `tests/terminals/test_host_client.py::test_raise_for_payload_preserves_structured_error`, `tests/servers/test_terminal_ws_create.py::test_create_result_carries_host_code_and_detail`.

### 1.3 Backend-aware reaper, attempt-owned timeouts, and settle CAS helpers [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor.py::reap_stale_pending_terminals`
- `src/gobby/agents/spawn_executor.py::kill_spawn_key`
- `src/gobby/agents/spawn_executor.py::_promote_prepared`
- `src/gobby/agents/spawn_executor.py::_runtime_spawn`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.kill`
- `src/gobby/terminals/web_spawn.py::spawn_web_terminal`
- `src/gobby/storage/terminals.py::TerminalManager.record_process`
- `src/gobby/storage/terminals.py::TerminalManager.bump_attempt_generation`
- `src/gobby/storage/terminals.py::TerminalManager.fail_pending_attempt`
- `src/gobby/storage/terminal_settlement.py`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.handle_spawn_prepared`
- `src/gobby/terminals/host_reconcile.py::reconcile_host_inventory`
- `src/gobby/terminals/host_reconcile.py::SupportsIdentityLookup`
- `tests/terminals/fakes.py::*` — scope-reason: `MemoryTerminalStore` implements the new CAS helpers
- `tests/agents/test_spawn_executor.py::*` — scope-reason: reaper and timeout cases move to attempt-generation ownership
- `tests/storage/test_terminals.py::*` — scope-reason: CAS helper suites for `settle_exit`, the `record_process` merge, and the reap-record merge
- `tests/terminals/test_host_manager.py::*` — scope-reason: prepared-spawn recording asserts the mandatory `host_terminal_id`
- `tests/terminals/test_tmux_runtime.py::*` — scope-reason: `_promote_prepared` and `spawn_web_terminal` cases settle through `settle_promotion`
- `tests/terminals/test_native_runtime.py::*` — scope-reason: `terminate_host_id` cases
- `tests/agents/test_srt_spawn.py::*` — scope-reason: `_runtime_spawn` fixtures follow attempt-generation ownership
- `tests/mcp_proxy/tools/spawn_agent/test_agy_gate.py::*` — scope-reason: `_runtime_spawn` fixtures follow attempt-generation ownership

`reap_stale_pending_terminals` terminates by backend identity: tmux rows through
`kill_spawn_key`, native rows through a new
`NativeTerminalRuntime.terminate_host_id(host_terminal_id, host_epoch)` that wraps `kill`
with the host id from `process["host_terminal_id"]` and the row's recorded `host_epoch`;
a native row without `host_terminal_id` is failed without a kill. Host ids restart at
`ht-1` in every new host (`HostState::next_host_id`), so after a 1.2 respawn an old row's
id can name a different live terminal; `terminate_host_id` therefore compares the
captured epoch with the manager's current epoch before any kill and, on a mismatch,
returns `HostEpochMismatch` without touching the host, and the caller fails the row
`host_epoch_changed` (1.2 table) because the old host and its PTY are already gone.
Every delayed native cleanup (the reaper, the agent spawn timeout, the web spawn
timeout, and `_failure_cleanup`) carries that captured `(host_terminal_id, host_epoch)`
pair from dispatch time; none re-reads the manager's epoch at fire time, and no new
lock is added. A stale native row whose host is
unreachable (`terminate_host_id` raises `HostUnavailableError`) is left pending for the
next sweep or the 3.2 list cut, because its PTY may still be alive; it is never failed
blind. A tmux retry whose `create_session` fails with a duplicate-session error kills
the `spawn_key` session before failing the attempt, so no session named `spawn_key`
survives. The spawn timeout done-callback
captures `(terminal_id, attempt_generation, attempt_started_at)` and calls
`fail_pending_attempt` with them, so a retried attempt is never failed by its
predecessor's timer; the `spawn_web_terminal` timeout callback captures the same triple
plus the host id and host epoch it created and terminates only that captured resource
before settling.
New `TerminalManager` members: `settle_lock(terminal_id)` returns a
refcounted `asyncio.Lock` cell released when its count reaches zero;
`settle_exit(terminal_id, host_terminal_id)` moves pending to exited or live to exited
only when the recorded host id matches; `merge_process_reap_record(terminal_id, pgid,
start_time)` merges only those two keys; `retry_attempt_unsettled(terminal_id,
attempt_generation)` is the CAS used by resume. `record_process` becomes an attempt-CAS
merge that requires `host_terminal_id`. `bump_attempt_generation` drops the
`host_terminal_id` key. Move the attempt and settlement CAS members
(`fail_pending_attempt`, `bump_attempt_generation`, `record_process`, and the new
`settle_lock`, `settle_exit`, `merge_process_reap_record`, `retry_attempt_unsettled`)
out of `terminals.py` into a `TerminalSettlementMixin` in the new
`src/gobby/storage/terminal_settlement.py` that `TerminalManager` inherits, so
`terminals.py` drops below 800 lines. A `settle_promotion(manager, terminal_id, ...)` helper in
`spawn_executor.py` runs `promote_to_live` under `settle_lock` and is imported by
`web_spawn.py` and `_promote_prepared`. `reconcile_host_inventory` records prepared
rows through `merge_process_reap_record` and `SupportsIdentityLookup` declares the
new members.

**Acceptance:**

- 1.3.1 - A stale native pending row is reaped through `terminate_host_id` with its recorded host id and host epoch and a stale tmux row through `kill_spawn_key`; a native row lacking `host_terminal_id` is failed with no kill call. test: `tests/agents/test_spawn_executor.py::test_reaper_terminates_by_backend_identity`.
- 1.3.9 - With a pending row holding `ht-1` from host epoch A, the host respawns as epoch B and allocates `ht-1` to a new live terminal; the paused reaper, the agent timeout callback, and the web timeout callback each refuse to kill (`HostEpochMismatch`, zero `kill` requests reach the host), epoch B's `ht-1` stays live, and the old row is failed `host_epoch_changed`. test: `tests/agents/test_spawn_executor.py::test_delayed_kill_refuses_reused_host_id_after_respawn`, `tests/terminals/test_native_runtime.py::test_terminate_host_id_requires_matching_epoch`.
- 1.3.2 - A spawn timeout firing after `bump_attempt_generation` does not fail the newer attempt, on both the agent and the web spawn path. test: `tests/agents/test_spawn_executor.py::test_timeout_callback_is_owned_by_attempt_generation`.
- 1.3.3 - `settle_exit` moves pending to exited and live to exited only when `host_terminal_id` matches and is a no-op otherwise; concurrent `settle_promotion` and `settle_exit` on one row serialize under `settle_lock` and leave exactly one terminal state. test: `tests/storage/test_terminals.py::test_settle_exit_is_guarded_cas`, `tests/storage/test_terminals.py::test_settle_lock_serializes_promotion_and_exit`.
- 1.3.4 - `record_process` without `host_terminal_id` raises `ValueError`, with it merges only onto the current attempt, and `merge_process_reap_record` never touches other keys. test: `tests/storage/test_terminals.py::test_record_process_is_attempt_cas_merge`.
- 1.3.5 - `bump_attempt_generation` removes `host_terminal_id` from `process` and `retry_attempt_unsettled` refuses a settled row. test: `tests/storage/test_terminals.py::test_bump_attempt_drops_host_id_and_retry_refuses_settled`.
- 1.3.6 - A stale native pending row whose `terminate_host_id` raises `HostUnavailableError` stays pending with its process record intact and is reaped on the next sweep once the host answers. test: `tests/agents/test_spawn_executor.py::test_reaper_leaves_unreachable_native_row_pending`.
- 1.3.7 - A tmux retry whose `create_session` reports the session name pre-existing kills the `spawn_key` session before the attempt is failed, and no session named `spawn_key` survives. test: `tests/agents/test_spawn_executor.py::test_tmux_retry_kills_duplicate_session_before_failing`.
- 1.3.8 - Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P2: Host control safety and commit barrier
`kind: framing`

**Goal**: the host refuses every unsafe operation with a typed reason, its state
module is under the ceiling, and a committed spawn is proof of exec.

### 2.1 Split `state.rs` and guard native-only host operations [category: refactor]
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/state.rs::*` — scope-reason: `HostState::spawn_commit`, `kill`, `snapshot`, `expire_prepared`, `broadcast_frames`, and `lag_timeout` move to `host/native_ops.rs`; the file drops below 600 lines
- `crates/gterminal/src/host/native_ops.rs`
- `crates/gterminal/src/host/native_ops/tests.rs`
- `crates/gterminal/src/host/control.rs::handle_connection`
- `crates/gterminal/src/host/control.rs::dispatch`
- `crates/gterminal/src/host/mod.rs::run`
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: `control_overflow`, `not_native`, and frame-task-exit cases join the protocol suite
- `crates/gterminal/tests/frame_producer.rs::*` — scope-reason: the closed-channel exit case
- `crates/gterminal/tests/embed.rs::*` — scope-reason: the reaped-observer typed close case
- `crates/gterminal/tests/source_size.rs::no_src_file_at_or_above_1000_lines`

Split `state.rs`: move the native-only operations into the new `native_ops.rs` as an
`impl HostState` block. `kill_group(pgid, sig)` in `native_ops.rs` is the only
`libc::killpg` call site and refuses `pgid <= 0` with `Err(InvalidPgid)` before touching
libc. Every native-only verb (`kill`, `write`, `write_paste`, `resize`, `snapshot`,
`reserve_observer`, `release_observer`) checks the slot's backend first and answers
`not_native` against a tmux-backed terminal before any state change, so the process
group, the PTY, and the observer table are untouched. `snapshot` truncates scrollback at
its byte cap on a char boundary through a new `trim_to_char_boundary` (also used for
error detail strings), so the payload is always valid UTF-8. A reaped observer's frame
connection receives a typed `observer_reaped` close and its task exits. Add
`read_request` to `control.rs`: it reads with `read_until` on a reader wrapped in
`take(MAX_CONTROL_LINE + 1)`, so a peer that never sends a newline is bounded to one
allocation of the cap, and answers `{"error":"control_overflow"}` then closes before any
JSON parse. The frame broadcast task exits when `recv_opt` returns `None` instead of
spinning. The three `unused_variables`/`unused_mut` allows in `state.rs` are removed
during the extraction (the variables are used or dropped), so 4.2's lint guard never
needs to touch `state.rs`. `source_size.rs` already enforces the ceiling for every
`src/` file and needs no change.

**Acceptance:**

- 2.1.1 - `kill_group` is the only `killpg` call in `crates/gterminal/src/host/` and returns `InvalidPgid` for `pgid <= 0` without calling into libc. test: `crates/gterminal/src/host/native_ops/tests.rs::kill_group_refuses_non_positive_pgid`.
- 2.1.2 - `kill`, `write`, `write_paste`, `resize`, `snapshot`, `reserve_observer`, and `release_observer` against a tmux observer slot each answer `not_native`, the host process group and observer table are untouched, and `ping` keeps answering. test: `crates/gterminal/tests/control_protocol.rs::native_verbs_refuse_tmux_terminals`.
- 2.1.3 - A control line of `MAX_CONTROL_LINE + 1` bytes is answered `control_overflow` and the connection closes without parsing; a peer that sends more than the cap with no newline is disconnected with the same error and bounded memory. test: `crates/gterminal/tests/control_protocol.rs::oversize_control_line_is_refused_before_parse`.
- 2.1.4 - `host/state.rs` is under 600 lines and every `crates/gterminal/src` file stays under the ceiling. test: `crates/gterminal/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.
- 2.1.5 - Dropping the frame sender ends the broadcast task within one ticker interval. test: `crates/gterminal/tests/frame_producer.rs::broadcast_task_exits_on_closed_channel`.
- 2.1.6 - Snapshot truncation of multibyte scrollback never panics and returns valid UTF-8 within the byte cap with the truncation flag set. test: `crates/gterminal/tests/control_protocol.rs::snapshot_truncates_on_char_boundaries`.
- 2.1.7 - After an observer is reaped its frame connection receives a typed `observer_reaped` close and the task exits instead of spinning. test: `crates/gterminal/tests/embed.rs::reaped_observer_frame_task_exits`.
- 2.1.8 - Guard set G groups 3, 4, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 2.2 Replace the FIFO gate with a `gterm gate` exec-status barrier [category: code] (depends: 2.1, 1.3)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/gate.rs`
- `crates/gterminal/src/host/spawn.rs::spawn_prepared`
- `crates/gterminal/src/host/spawn.rs::PreparedChild::commit`
- `crates/gterminal/src/host/spawn.rs::PreparedChild::drop`
- `crates/gterminal/src/pane/runtime.rs::*` — scope-reason: the child waiter started by `spawn_command_builder` and `from_master_fd` retains the wait result on `PaneRuntime` and a new `child_exit` accessor exposes it
- `crates/gterminal/src/host/native_ops.rs`
- `crates/gterminal/src/gterm.rs`
- `src/gobby/config/terminal_host.py::TerminalHostConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated checked-in contract for `commit_deadline_ms`
- `src/gobby/terminals/host_client.py::HostClient.spawn_commit`
- `tests/terminals/test_native_runtime.py::*` — scope-reason: `commit_spawn` forwards `commit_deadline_ms`
- `tests/config/test_terminal_host.py::*` — scope-reason: `commit_deadline_ms` bounds
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.commit_spawn`
- `crates/gterminal/tests/host_lifecycle.rs::*` — scope-reason: exec-failure, timeout, and malformed-status cases
- `tests/agents/test_native_spawn.py::*` — scope-reason: `commit_deadline_ms` forwarding and `exec_failed` mapping
- `src/gobby/config/app.py::*` — scope-reason: `TerminalHostConfig` composition re-verified; `commit_deadline_ms` carries a default
- `src/gobby/runner.py::*` — scope-reason: `TerminalHostConfig` consumer re-verified; `commit_deadline_ms` carries a default
- `tests/test_runner_lifecycle_processes.py::*` — scope-reason: host config fixtures re-verified against `commit_deadline_ms`

`gterm gate -- <argv>` is spawned in the PTY with the gate pipe on fd 3 and the
exec-status pipe on fd 4. It blocks on fd 3, resolves `argv[0]` against `PATH` itself
(a path containing `/` is used as given), and hands off with `execve`, never `execvp`,
because `execvp` runs `/bin/sh` on an image the kernel rejects with `ENOEXEC` (exec(3))
and that would turn a required `exec_failed:ENOEXEC` into a running shell. Every
deliberate pre-exec exit path in the gate (argument marshalling, `setsid`, `dup2`,
`PATH` resolution, `execve`) writes
`{"code":"<errno name>","detail":"<strerror>","stage":"<step>"}` to fd 4 and exits 127,
so no reported failure is mistaken for a commit. The host's `spawn_commit` in
`native_ops.rs` writes the gate byte, then reads fd 4 until EOF or the JSON status,
bounded by `commit_deadline_ms` (request field, default 30 000, minimum 1 000). The
status pipe guarantees exactly one fact: no pre-exec error was reported. EOF without a
status is therefore answered `committed` whether or not the child is still running
when the host reads it. A fast image such as `/usr/bin/true` can exec, close fd 4
through CLOEXEC, and exit before the host reads, and a child killed by a signal closes
fd 4 the same way whether the signal lands before or after `execve`; those cases are
observably identical and all settle as exits. `PaneRuntime`'s existing child waiter,
started before commit in both `spawn_command_builder` and `from_master_fd`, is the
sole reap owner: instead of discarding the status it retains the wait result (exit
code or terminating signal) on the runtime and exposes it through a new
`PaneRuntime::child_exit` accessor, keeping the completion flag that pane shutdown
already reads. A child already reaped at commit time therefore has its result
available immediately and the host's exit path settles the terminal from it (3.2's
watcher observes that retained result and never calls `wait` or `waitpid` itself), so
the host never infers a launch failure from an exit and no second reaper exists. Expiry answers `exec_timeout` and
unparseable bytes answer `malformed_status`. On `exec_failed`, `exec_timeout`, and
`malformed_status` the child is killed and the prepared slot removed, so no prepared or
committed record survives. A prepared child whose host dies exits within 1 s on
gate-pipe EOF.
`TerminalHostConfig.commit_deadline_ms` is forwarded by `HostClient.spawn_commit`;
regenerate `runtime_config_contract.json` for it. `spawn.rs` no longer references
`mkfifo` or `/bin/sh`.

**Acceptance:**

- 2.2.1 - Committing a prepared spawn whose argv names a missing binary answers `exec_failed` with `code="ENOENT"`, a file without the execute bit `code="EACCES"`, and an executable text file with no recognized header `code="ENOEXEC"` (no shell is ever started for it), each with `detail` and `stage:"execve"`, and no prepared or committed slot survives in `list`. test: `crates/gterminal/tests/host_lifecycle.rs::commit_reports_exec_failure_with_errno`.
- 2.2.2 - A gate child that never execs answers `exec_timeout` after `commit_deadline_ms` and is killed; a truncated status answers `malformed_status`; both leave no slot in `list`. test: `crates/gterminal/tests/host_lifecycle.rs::commit_times_out_and_rejects_malformed_status`.
- 2.2.3 - A successful commit returns only after exec (the child's process name equals argv[0]) and `spawn.rs` no longer references `mkfifo` or `/bin/sh`. test: `crates/gterminal/tests/host_lifecycle.rs::commit_returns_after_exec`.
- 2.2.4 - `NativeTerminalRuntime.commit_spawn` forwards `commit_deadline_ms` from config and maps `exec_failed` to `SpawnResult.error="exec_failed:<code>"` for errno codes, `exec_timeout` to `"exec_timeout"`, and `malformed_status` to `"malformed_status"`, with `error_detail` from the host's `detail` and `stage`. test: `tests/agents/test_native_spawn.py::test_commit_forwards_deadline_and_maps_exec_failed`.
- 2.2.5 - A gate child killed after the gate byte and before `execve` (fault injected through a `GTERM_GATE_FAULT` environment value the gate honours only under `cfg(test)`-built helpers) is answered `committed` and then settled as an exit by that signal with no prepared slot left; a fault injected at `setsid`, `dup2`, or `PATH` resolution answers `exec_failed` with that `stage` and no slot survives. test: `crates/gterminal/tests/host_lifecycle.rs::preexec_signal_settles_as_exit_and_injected_faults_never_commit`.
- 2.2.8 - A committed `/usr/bin/true` that exits before the host reads fd 4 is answered `committed` and settles exited with status 0, and `/usr/bin/false` settles with status 1; neither is answered `exec_failed`, and after settlement no slot survives in `list`. test: `crates/gterminal/tests/host_lifecycle.rs::fast_exit_after_exec_is_committed_then_exited`.
- 2.2.6 - Killing the host while a prepared child is waiting makes the child exit within 1 s on gate-pipe EOF, leaving no blocked child behind. test: `crates/gterminal/tests/host_lifecycle.rs::host_death_releases_prepared_child`.
- 2.2.7 - Guard set G groups 1, 2, 3, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P3: Host lifecycle and client correctness
`kind: framing`

**Goal**: the host and daemon shut down honestly, tests never leak hosts, and the
daemon's control client is request-correlated, single-reader, and event-driven.

### 3.1 Real host drain, daemon shutdown escalation, and test process hygiene [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/mod.rs::run`
- `crates/gterminal/src/host/native_ops.rs`
- `crates/gterminal/src/host/control.rs::dispatch`
- `crates/gterminal/tests/host_lifecycle.rs::*` — scope-reason: socket-dir-vanish exit and drain-grace cases; every host is owned by `HostProc`
- `crates/gterminal/tests/host_support/mod.rs::spawn_host`
- `crates/gterminal/tests/host_support/mod.rs::spawn_host_with_args`
- `crates/gterminal/tests/embed_support/mod.rs::HostProc`
- `src/gobby/terminals/host_manager.py::TerminalHostManager._host_shutdown`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.stop`
- `tests/terminals/conftest.py::*` — scope-reason: session-scoped `gterm host` leak check and pidfile finalizers
- `tests/terminals/host_fakes.py::*` — scope-reason: the fake control server drops the `attach` verb
- `tests/terminals/test_host_manager.py::*` — scope-reason: shutdown escalation cases
- `tests/terminals/test_host_shutdown_preservation.py::*` — scope-reason: preserved-host cases assert no escalation past the RPC rung
- `crates/gterminal/tests/frame_protocol.rs::*` — scope-reason: migrate the typed host owner and all affected child operations to HostProc
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: migrate authed and all affected child operations with the shared HostProc return type
- `crates/gterminal/tests/host_support/mod.rs::wait_exit`
- `src/gobby/runner.py::*` — scope-reason: the `TerminalHostManager.stop` call site follows the escalating shutdown
- `tests/test_runner_lifecycle_processes.py::*` — scope-reason: stop fixtures follow the escalating shutdown

The host `run()` loop stats its socket directory every health tick and exits with
`socket_dir_removed` when it is gone. `host_shutdown{grace_ms}` performs a real drain in
`native_ops.rs`: set `draining`, SIGHUP every live pgid through `kill_group`, wait up to
`grace_ms`, SIGKILL survivors, then exit; a second `host_shutdown` during drain is
idempotent. `dispatch` drops the fake `attach` verb. The daemon's `_host_shutdown`
escalates RPC, then SIGTERM after `shutdown_grace_seconds`, then SIGKILL after another
grace, recording the rung that succeeded in `last_error`. Move `HostProc` (the RAII
kill-on-drop owner already in `embed_support`) into shared use: `host_support::spawn_host`
and `spawn_host_with_args` return it. pytest gets pidfile finalizers and a session-scoped
fixture asserting the set of `gterm host` pids after the session equals the set before
(Guard set G group 7, automated).

**Acceptance:**

- 3.1.1 - Removing the socket directory makes the host exit within two health ticks with reason `socket_dir_removed`. test: `crates/gterminal/tests/host_lifecycle.rs::host_exits_when_socket_dir_vanishes`.
- 3.1.2 - `host_shutdown{grace_ms:500}` SIGHUPs a child that traps SIGHUP, waits about 500 ms, then SIGKILLs it; a child that exits on SIGHUP is not SIGKILLed. test: `crates/gterminal/tests/host_lifecycle.rs::drain_honours_grace_then_kills`.
- 3.1.3 - The control protocol has no `attach` verb and the fake host answers `unknown_verb` for it. test: `tests/terminals/test_host_manager.py::test_fake_host_has_no_attach_verb`.
- 3.1.4 - `_host_shutdown` escalates RPC, SIGTERM, SIGKILL, each after `shutdown_grace_seconds`, and stops at the first rung that ends the process. test: `tests/terminals/test_host_manager.py::test_host_shutdown_escalates`.
- 3.1.5 - The `tests/terminals` session leaves no `gterm host` process that it created, and every Rust host test owns its host through `HostProc`. test: `tests/terminals/conftest.py::_assert_no_leaked_hosts`. symbol: `HostProc`.
- 3.1.6 - Guard set G groups 2, 3, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 3.2 Request correlation, single reader, event stream, and native exit events [category: code] (depends: 2.2, 3.1, 1.3)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/control.rs::ControlRequest`
- `crates/gterminal/src/host/control.rs::handle_connection`
- `crates/gterminal/src/host/control.rs::dispatch`
- `crates/gterminal/src/host/control.rs::with_id`
- `crates/gterminal/src/host/control.rs::recv_event`
- `crates/gterminal/src/host/events.rs`
- `crates/gterminal/src/host/native_ops.rs`
- `crates/gterminal/tests/wire_golden.rs::*` — scope-reason: goldens regenerate for `id`, `since`, `gap`, and `terminal_exited`
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: id, event-stream, and exit-event cases
- `src/gobby/terminals/host_client.py::HostClient.__init__`
- `src/gobby/terminals/host_client.py::HostClient.connect`
- `src/gobby/terminals/host_client.py::HostClient.close`
- `src/gobby/terminals/host_client.py::HostClient.read_payload`
- `src/gobby/terminals/host_client.py::HostClient._roundtrip`
- `src/gobby/terminals/host_client.py::HostClient.subscribe_events`
- `src/gobby/terminals/host_client.py::HostClient.reconnect`
- `src/gobby/terminals/host_events.py`
- `src/gobby/terminals/frame_client.py::*` — scope-reason: the frame client shares `HostClient`'s connect and close lifecycle and must stop the reader task on close
- `tests/terminals/test_frame_client.py::*` — scope-reason: close cases assert the reader task ends
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.is_live`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.resize`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.terminate`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.__init__`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime.attach_locator`
- `src/gobby/runner_lifecycle_subsystems.py::_start_terminal_host`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.__init__`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.start`
- `src/gobby/terminals/host_manager.py::TerminalHostManager._connect`
- `src/gobby/terminals/host_manager.py::TerminalHostManager._close_client`
- `src/gobby/terminals/host_manager.py::TerminalHostManager._health_loop`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.reconcile`
- `src/gobby/terminals/host_manager.py::TerminalHostManager.reap_recorded_process`
- `src/gobby/terminals/host_reconcile.py::reconcile_host_inventory`
- `src/gobby/terminals/host_reap.py::reap_recorded_process`
- `src/gobby/runner_init/orchestration.py::init_orchestration`
- `src/gobby/runner_init/terminal_wiring.py`
- `tests/terminals/test_composition_roots.py::*` — scope-reason: source assertions follow the wiring move
- `tests/terminals/test_host_client.py`
- `tests/terminals/test_native_runtime.py::*` — scope-reason: `resize` and `terminate` reconnect with real arguments
- `tests/terminals/test_runtime_contract.py::*` — scope-reason: `is_live` requires the epoch across backends
- `tests/test_runner_lifecycle_subsystems.py::*` — scope-reason: the `_frame_host_epoch` poke assertion is removed
- `tests/terminals/test_host_manager.py::*` — scope-reason: event recovery and indeterminate settlement
- `tests/terminals/test_wire_golden.py::*` — scope-reason: regenerated goldens
- `src/gobby/runner.py::*` — scope-reason: `TerminalHostManager` construction and `start` call sites follow the event-reader wiring
- `tests/test_runner_lifecycle_processes.py::*` — scope-reason: host-manager construction and start fixtures follow the event-reader wiring
- `src/gobby/runner_init/servers.py::*` — scope-reason: the `HostClient.close` call site follows the single-reader client
- `src/gobby/runner_init/__init__.py::*` — scope-reason: re-exports `init_orchestration`; re-verified after the terminal wiring moves out
- `tests/test_runner_lifecycle.py::*` — scope-reason: `init_orchestration` fixtures follow the terminal wiring move

Every control request carries `id`; `missing_id` and `duplicate_id` are answered
without dispatch. `HostClient` runs one reader task per connection that routes
responses to futures by id and events to `open_event_stream()` subscribers
(`host_events.py` holds the event dataclasses); any read failure resolves every
pending future with `HostConnectionLost`. `connect` opens the socket with
`asyncio.open_unix_connection(path, limit=MAX_CONTROL_LINE + 1)` so the stream reader's
`readline()` decodes a reply up to the cap (a 1 MiB reply, above asyncio's 64 KiB
default, included) and raises `LimitOverrunError` on a longer line, which the reader
turns into `HostConnectionLost`. On the host side `handle_connection` no longer
dispatches inline: it reads lines and spawns each dispatch as a task under a
per-connection in-flight cap (`MAX_INFLIGHT_PER_CONNECTION`, 64, refusing beyond it
with `too_many_inflight`), and one writer task per connection serialises responses and
events, so a `spawn_commit` waiting on its deadline never blocks `ping`, `list`,
`resize`, or `kill` on the same connection; the operation-seq ledger entry is assigned
when a dispatch starts, so ledger order is preserved. Native `terminal_exited{terminal_id,
host_terminal_id, exit_code}` events come from a watcher installed in
`native_ops.rs` at commit that awaits the slot runtime's retained
`PaneRuntime::child_exit` result (2.2) and never waits on the child itself, carried
by the new `events.rs`; a child already reaped when commit answers (2.2's fast-exit
case) produces its event immediately from that retained result; a tmux observer whose
pane dies never produces a control-plane event, its exit travels on the frame stream
only. The manager's event reader keeps `(last_event_epoch, last_event_seq)`,
resubscribes with `since`, and on `{"gap":true}` runs `list` as the cut and settles rows
from it: a `commit_indeterminate` row is promoted when listed committed, killed by its
host id and failed `not_committed` when listed prepared, and failed `not_found` when
absent. A gap response keeps the subscription registered before `list`; the manager
buffers events while `list` is in flight in a buffer bounded by `GAP_BUFFER_ENTRIES`
(4 096) and `GAP_BUFFER_BYTES` (4 MiB), treats the list reply's `(epoch, seq)` as the
cut, drops buffered events at or below it, replays later events once in order, and
resumes from that cursor; if the buffer overflows before the list reply, the manager
discards it and runs the cut again after the reply, repeating until one cycle completes
under the bound; sequence numbers are never compared across epochs. Before `reconnect`
accepts new requests, the
old reader fails and clears only its own pending futures and is cancelled and joined,
leaving exactly one reader per connection. The event reader joins `ensure_restart` when
the host is gone and exits cleanly on `HostManagerStopped`. `reap_recorded_process`
runs under `asyncio.to_thread`. `is_live` requires `host_epoch` on the row. Delete the
`_frame_host_epoch` attribute and its poke in `_start_terminal_host`; `attach_locator`
takes the epoch from the manager. `resize` and `terminate` call
`reconnect(socket_path, expected_epoch)` with the manager's socket path and the row's
recorded `host_epoch`, never the manager's current epoch, so a row from an earlier host
epoch is refused `host_epoch_changed` instead of being resized or killed against the
replacement host's reused id (1.3). `reconcile_host_inventory`
catches only a new `ReconcileError`. Move the terminal-host wiring block of
`init_orchestration` (host manager, runtime registry, coordinator, services) into the
new `terminal_wiring.py` so `orchestration.py` drops below 860 lines.

**Acceptance:**

- 3.2.1 - A request without `id` is answered `missing_id`; two in-flight requests with one id get `duplicate_id` on the second; every response echoes `id`. test: `crates/gterminal/tests/control_protocol.rs::requests_require_unique_ids`.
- 3.2.2 - A committed child that exits produces exactly one `terminal_exited` event with its `exit_code` on the event stream, including a child already reaped when commit answered, and the request writer never carries events. test: `crates/gterminal/tests/control_protocol.rs::child_exit_emits_terminal_exited_on_event_stream`.
- 3.2.3 - `HostClient` serves interleaved responses to concurrent callers by id through one reader task, and a broken connection resolves every pending call with `HostConnectionLost`. test: `tests/terminals/test_host_client.py::test_reader_task_correlates_and_fails_pending_on_loss`.
- 3.2.4 - After an event gap the manager lists the host, promotes a `commit_indeterminate` row that is listed committed, kills a listed-but-prepared slot by its host id and fails the row `not_committed`, and fails an absent one `not_found`. test: `tests/terminals/test_host_manager.py::test_gap_settles_indeterminate_from_list`.
- 3.2.5 - `is_live` returns `False` for a native row without `host_epoch`; no `_frame_host_epoch` attribute exists; `resize` and `terminate` reconnect with the manager's socket path and the row's `host_epoch`, and a row from an earlier epoch is refused `host_epoch_changed` with no request sent. test: `tests/agents/test_native_spawn.py::test_is_live_requires_epoch_and_reconnect_has_arguments`.
- 3.2.6 - `reconcile_host_inventory` lets a non-`ReconcileError` exception propagate and reaping never blocks the loop for longer than one scheduler tick. test: `tests/terminals/test_host_manager.py::test_reconcile_catches_only_typed_errors`, `tests/terminals/test_host_manager.py::test_reap_runs_off_loop`.
- 3.2.7 - Wire goldens match the Rust emitter for `id`, `since`, `gap`, and `terminal_exited`, and the terminal wiring is built once in `terminal_wiring.py`. test: `tests/terminals/test_wire_golden.py::test_control_goldens_match_rust_emitter`, `tests/terminals/test_composition_roots.py::test_orchestration_builds_terminal_services_once`.
- 3.2.8 - With event production faster than the ring capacity throughout recovery, one subscribe-before-list cycle converges, delivers every event after the list cut once and in order, and never enters a second gap. test: `tests/terminals/test_host_manager.py::test_gap_recovery_converges_under_ring_churn`.
- 3.2.9 - `reconnect` and `close` fail old pending calls once, admit no stale reply into the replacement reader generation, and leave zero old reader tasks; cancelling one caller leaves unrelated futures untouched. test: `tests/terminals/test_host_client.py::test_reconnect_replaces_reader_generation_atomically`.
- 3.2.10 - After a host crash the event reader joins the manager's single restart, and during drain or stop it exits on `HostManagerStopped` without moving its cursor or spawning a host. test: `tests/terminals/test_host_manager.py::test_event_reader_joins_singleflight_and_stops_cleanly`.
- 3.2.11 - A 1 MiB control reply is decoded on the initial connection, on a reconnect replacement, and on the event stream, and a line over `MAX_CONTROL_LINE` resolves every pending call with `HostConnectionLost`; `readline` is never called with a `limit` argument. test: `tests/terminals/test_host_client.py::test_reader_decodes_large_reply_and_fails_over_cap`.
- 3.2.12 - While a `spawn_commit` on one connection waits on its deadline, `ping`, `list`, `resize`, and `kill` on the same connection are answered, responses carry their own ids, the ledger stays totally ordered, and the 65th in-flight request is refused `too_many_inflight`. test: `crates/gterminal/tests/control_protocol.rs::commit_wait_does_not_block_other_requests`.
- 3.2.13 - With the gap buffer overflowing before the list reply, the manager discards the buffer, repeats the cut after the reply, converges without applying any stale event, and ends subscribed at the final list cursor. test: `tests/terminals/test_host_manager.py::test_gap_buffer_overflow_repeats_cut`.
- 3.2.14 - A tmux observer whose pane dies emits no control-plane event while a control subscriber is attached; the exit reaches only the slot's frame stream. test: `crates/gterminal/tests/control_protocol.rs::tmux_pane_death_emits_no_control_event`.
- 3.2.15 - Guard set G groups 1, 2, 3, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P4: CI, packaging, and installer
`kind: framing`

**Goal**: the crate builds and lints honestly in CI, PTY tests run in their serial
group, release assertions match the tree, and the installer knows which managed
binaries are unpublished.

### 4.1 Vendor build gating and release assertions [category: config]
`kind: deliverable`

Targets:
- `tests/gterminal/test_vendor_layer.py::*` — scope-reason: zig-dependent tests skip unless `GOBBY_RUN_VENDOR_BUILD=1`; the portable-pty LICENSE assertion is dropped
- `tests/gterminal/test_release_assertions.py`
- `.github/workflows/release-gterminal.yml`

`test_vendor_layer.py` skips zig-requiring cases unless `GOBBY_RUN_VENDOR_BUILD=1`
(line 243's zig assertion included). `release-gterminal.yml` stops requiring
`vendor/portable-pty/LICENSE.md` and asserts the existing `crates/gterminal/NOTICE.md`
instead. The new `test_release_assertions.py` parses the workflow's package assertion
list and checks every path against `cargo package -p gobby-terminal --list --no-verify`,
and asserts that `cargo package -p gobby-terminal --no-verify` and the same for
`gobby-client` emit no licence warning; both cases skip unless `GOBBY_RUN_VENDOR_BUILD=1`
because packaging needs the vendor tree. Landed and only cited here: `rust-ci.yml`
already runs the Zig-free default-features gate (line 108) and the Windows cross-target
clippy for `x86_64-pc-windows-msvc` (lines 133–138), and `crates/gterminal/NOTICE.md`
already records the vendored third-party licences.

**Acceptance:**

- 4.1.1 - Without `GOBBY_RUN_VENDOR_BUILD` the vendor suite collects and reports its zig cases as skipped, never failed. test: `tests/gterminal/test_vendor_layer.py::test_zig_cases_skip_without_opt_in`.
- 4.1.2 - The release workflow asserts `NOTICE.md` and no longer references `portable-pty/LICENSE.md`. file: `.github/workflows/release-gterminal.yml`.
- 4.1.3 - Every path the release workflow's package assertion requires is present in `cargo package -p gobby-terminal --list --no-verify`, exercised by a test that runs the same loop against the real listing. test: `tests/gterminal/test_release_assertions.py::test_release_assertion_paths_are_packaged`.
- 4.1.4 - `cargo package --no-verify` for `gobby-terminal` and `gobby-client` emits no licence warning. test: `tests/gterminal/test_release_assertions.py::test_package_emits_no_license_warning`.
- 4.1.5 - Guard set G groups 1 and 5 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 4.2 Lint policy without blanket allows [category: refactor] (depends: 2.1, 3.1)
`kind: deliverable`

Targets:
- `crates/gterminal/src/lib.rs::*` — scope-reason: the crate-level `dead_code, unused_imports, private_interfaces, clippy::all` allow is removed
- `crates/gterminal/src/ghostty/mod.rs::*` — scope-reason: the bindgen module keeps one scoped allow with a `// reason:` line
- `crates/gterminal/src/ghostty/bindings.rs::*` — scope-reason: generated bindings keep their allow with a `// reason:` line
- `crates/gterminal/tests/carve_guard.rs::*` — scope-reason: `no_blanket_lint_allows` scans `src/`
- `crates/gterminal/src/host/embed.rs::*` — scope-reason: warnings surfaced by the blanket removal are fixed in place
- `crates/gterminal/src/host/frames.rs::*` — scope-reason: two `dead_code` allows removed with their dead items; surfaced warnings fixed
- `crates/gterminal/src/host/mod.rs::run`
- `crates/gterminal/src/host/mod.rs::HostArgs`
- `crates/gterminal/src/input/encode.rs::*` — scope-reason: six `dead_code` allows resolved by wiring or deleting the helpers
- `crates/gterminal/src/input/mod.rs::*` — scope-reason: the `unused_imports` allow is removed with the imports
- `crates/gterminal/src/input/model.rs::*` — scope-reason: the reserved `dead_code` item is deleted; surfaced warnings fixed
- `crates/gterminal/src/input/parse.rs::*` — scope-reason: seven `dead_code` allows on reserved parser items are deleted with the items
- `crates/gterminal/src/ipc.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/kitty_graphics/host_paint.rs::*` — scope-reason: the module-level `dead_code, unused_imports` allow is removed and its residue fixed
- `crates/gterminal/src/kitty_graphics/host_stream.rs::*` — scope-reason: the module-level `dead_code, unused_imports` allow is removed and its residue fixed
- `crates/gterminal/src/pane/osc.rs::*` — scope-reason: two `dead_code` allows removed
- `crates/gterminal/src/pane/runtime.rs::*` — scope-reason: four `too_many_arguments` allows gain `// reason:` lines
- `crates/gterminal/src/platform/fallback.rs::*` — scope-reason: the `cfg_attr` `dead_code` allow is replaced by a `cfg`-gated item
- `crates/gterminal/src/platform/mod.rs::*` — scope-reason: the `cfg_attr` `dead_code` allow is replaced and surfaced warnings fixed
- `crates/gterminal/src/platform/macos.rs::*` — scope-reason: surfaced warnings fixed; process inspection helpers move to `macos_process.rs`
- `crates/gterminal/src/platform/macos_process.rs`
- `crates/gterminal/src/platform/windows.rs::*` — scope-reason: the two `non_camel_case_types` allows gain `// reason:` lines; daemon launch helpers move to `windows_daemon.rs`
- `crates/gterminal/src/platform/windows_daemon.rs`
- `crates/gterminal/src/platform/windows_process.rs::*` — scope-reason: the `cfg_attr` `dead_code` allow is replaced by a `cfg`-gated item
- `crates/gterminal/src/protocol/render_ansi.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/protocol/render_ansi_blit.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/protocol/wire_codec.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/protocol/wire_types.rs::*` — scope-reason: the `deprecated` allow gains a `// reason:` line; surfaced warnings fixed
- `crates/gterminal/src/pty/actor.rs::*` — scope-reason: the module-level `dead_code, unused_imports` allow and the item allow are removed with their residue
- `crates/gterminal/src/pty/actor/unix.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/pty/backend.rs::*` — scope-reason: the module-level `dead_code, unused_imports` allow is removed with its residue
- `crates/gterminal/src/pty/backend/unix.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/pty/fd.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/pty/mod.rs::*` — scope-reason: the module-level `dead_code, unused_imports` allow is removed with its residue
- `crates/gterminal/src/raw_input.rs::*` — scope-reason: the item `dead_code` allow and the `cfg_attr` allow are replaced by `cfg`-gated items
- `crates/gterminal/src/raw_input_framer.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/render_prof.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/selection.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/terminal_modes.rs::*` — scope-reason: surfaced warnings fixed in place
- `crates/gterminal/src/terminal_theme.rs::*` — scope-reason: surfaced warnings fixed in place

Blanket `#![allow(dead_code, unused_imports, private_interfaces, clippy::all)]` in
`lib.rs` is removed. A `cargo clippy -p gobby-terminal --all-targets -- --force-warn
dead_code --force-warn unused_imports --force-warn private_interfaces --force-warn
clippy::all` run on the landed tree (2026-09-10) shows what it hides: 163 warnings in 24
files; a textual sweep finds allows in 21 files outside `ghostty/`. Every one of those
files is a Target above and this leaf owns each fix: dead items are deleted or made
reachable (no `dead_code` allow survives outside `ghostty/`, so the reserved parser and
encoder helpers in `input/` are deleted under the no-backward-compatibility rule),
unused imports are removed, private-interface leaks become `pub(crate)`, clippy
findings are fixed in place, and `cfg_attr(..., allow(dead_code))` platform items become
`cfg`-gated items. `platform/macos.rs` (916 production lines) moves its process
inspection helpers (`process_group_pids`, `foreground_process_group_id`,
`foreground_process_group_id_for_tty_fd`, `process_argv0_name`, `kern_procargs2`,
`process_bsdinfo`, `comm_from_bsdinfo`, `process_argv`, `procargs2_argv_start`,
`skip_nul_strings`, `procargs2_argv`, `procargs2_env`, `process_cwd`,
`session_processes`) into the new `platform/macos_process.rs`; `platform/windows.rs`
(935) moves the daemon launch and job helpers (`launch_server_daemon_command`,
`launch_server_daemon_with_wmi` with its WMI structs, `windows_command_line`,
`effective_command_environment`, `windows_environment_key_cmp`,
`unicode_windows_value`, `current_process_is_in_job`,
`current_job_kills_processes_on_close`, `detach_server_daemon_command`,
`current_process_is_detached_server_daemon`) into the new `platform/windows_daemon.rs`;
both parents drop below 700 lines and their `tests.rs` modules follow the moved
symbols. `host/state.rs` is not touched here: prerequisite 2.1's extraction removes
its allows. Windows-only warnings surface in the landed cross-target clippy job in
`rust-ci.yml` and are fixed by this leaf. Each remaining allow names single lints and
carries `// reason: …`; the two ghostty bindgen modules are the only exemption.
`carve_guard::no_blanket_lint_allows` fails on any `clippy::all` or `dead_code` allow
outside `ghostty/` and on any allow without a reason line.

**Acceptance:**

- 4.2.1 - `cargo clippy -p gobby-terminal --all-targets --features vt-engine -- -D warnings` passes with no `clippy::all` or `dead_code` allow outside `src/ghostty/` and every remaining allow carrying a `// reason:` line, and the landed Windows cross-target clippy job in `rust-ci.yml` stays green. test: `crates/gterminal/tests/carve_guard.rs::no_blanket_lint_allows`.
- 4.2.3 - `platform/macos.rs` and `platform/windows.rs` are each under 700 production lines, `platform/macos_process.rs` owns the macOS process inspection helpers, and `platform/windows_daemon.rs` owns the Windows daemon launch helpers. file: `crates/gterminal/src/platform/macos_process.rs`. file: `crates/gterminal/src/platform/windows_daemon.rs`. test: `crates/gterminal/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.
- 4.2.2 - Guard set G groups 3, 4, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 4.3 nextest PTY group filter and private build env [category: config]
`kind: deliverable`

Targets:
- `.config/nextest.toml`
- `crates/gterminal/tests/build_env.rs::*` — scope-reason: cargo invocations from tests use a private `CARGO_TARGET_DIR`

The `gterm-pty` override filter becomes
`package(gobby-terminal) & (test(/pty::|pane::runtime/) | binary(frame_producer) | binary(host_lifecycle) | binary(control_protocol) | binary(frame_protocol) | binary(embed))`
with the group's `max-threads = 1` unchanged. `build_env.rs` gives test-driven cargo
invocations a private `CARGO_TARGET_DIR` under the scratch tree.

**Acceptance:**

- 4.3.1 - `cargo nextest list -p gobby-terminal --features vt-engine` places every `host_lifecycle`, `control_protocol`, `frame_producer`, `frame_protocol`, and `embed` test in the `gterm-pty` group. file: `.config/nextest.toml`.
- 4.3.2 - Test-driven cargo builds use a target dir outside the workspace `target/`. test: `crates/gterminal/tests/build_env.rs::build_env_uses_private_target_dir`.
- 4.3.3 - Guard set G groups 3 and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 4.4 Unpublished managed binaries in the installer [category: code]
`kind: deliverable`

Targets:
- `src/gobby/install/version_pins.py`
- `src/gobby/cli/install_setup_gterm.py::install_gterm`
- `src/gobby/cli/install_setup_gclient.py::install_gclient`
- `tests/install/test_version_pins.py::*` — scope-reason: published and unpublished cases
- `tests/cli/test_install_setup_gterm.py::*` — scope-reason: state-table cases
- `tests/cli/test_install_setup_gclient.py`

Add `UNPUBLISHED_MANAGED_BINS: frozenset[str]` and `is_published(name)` to
`version_pins.py`; add `ManagedBinaryReleaseMissing` to `install_setup_gterm.py`.
Both installers keep their existing `install_*(module, force=False)` signature. The
decision inputs are `published`, `present`, `version satisfies pin`, and `force`;
`force=True` treats a present binary as absent for the decision and never keeps it.
"Source build" is the existing `install_*_from_submodule` checkout build; "fetch chain"
is the existing `install_*_from_github`, `install_*_from_cargo_binstall`,
`install_*_from_cargo_install`, `install_*_from_cargo_git` order, stopping at the first
success. Installer state table, applied by both installers:

| published | present | version satisfies pin | force | action |
|---|---|---|---|---|
| no | yes | any | no | keep, stamp, `method="local"` |
| no | yes | any | yes | source build, else raise `ManagedBinaryReleaseMissing` |
| no | no | none | any | source build, else raise `ManagedBinaryReleaseMissing` |
| yes | yes | yes | no | keep, stamp |
| yes | yes | yes | yes | fetch chain |
| yes | yes | no | any | fetch chain |
| yes | no | none | any | fetch chain |

**Acceptance:**

- 4.4.1 - `is_published("gterm")` is `False` while `gterm` is in `UNPUBLISHED_MANAGED_BINS`, and the installer never calls the GitHub, binstall, or cargo-install fetchers for an unpublished binary. test: `tests/cli/test_install_setup_gterm.py::test_unpublished_binary_skips_remote_fetchers`.
- 4.4.2 - Table row `unpublished, absent`: with a workspace checkout the source build runs and is stamped; without one `ManagedBinaryReleaseMissing` is raised naming the binary, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_unpublished_absent_binary_builds_or_raises`, `tests/cli/test_install_setup_gclient.py::test_unpublished_absent_binary_builds_or_raises`.
- 4.4.3 - Table row `unpublished, present, force=False`: the binary is kept and stamped with `method="local"` regardless of version, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_unpublished_present_binary_is_kept`, `tests/cli/test_install_setup_gclient.py::test_unpublished_present_binary_is_kept`.
- 4.4.4 - Table row `published, present, satisfying, force=False`: the binary is kept and stamped without fetching, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_published_satisfying_binary_is_kept`, `tests/cli/test_install_setup_gclient.py::test_published_satisfying_binary_is_kept`.
- 4.4.5 - Table row `published, present, not satisfying`: the fetch chain runs in order github, binstall, cargo install, cargo git, stopping at the first success, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_published_binary_runs_fetch_chain`, `tests/cli/test_install_setup_gclient.py::test_published_binary_runs_fetch_chain`.
- 4.4.6 - Table row `published, absent`: the same ordered fetch chain runs for both installers, stopping at the first success. test: `tests/cli/test_install_setup_gterm.py::test_published_absent_binary_runs_fetch_chain`, `tests/cli/test_install_setup_gclient.py::test_published_absent_binary_runs_fetch_chain`.
- 4.4.7 - Table row `unpublished, present, force=True`: the present binary is not kept; the source build runs, else `ManagedBinaryReleaseMissing`, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_force_rebuilds_unpublished_present_binary`, `tests/cli/test_install_setup_gclient.py::test_force_rebuilds_unpublished_present_binary`.
- 4.4.8 - Table row `published, present, satisfying, force=True`: the present binary is not kept and the fetch chain runs, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_force_refetches_published_satisfying_binary`, `tests/cli/test_install_setup_gclient.py::test_force_refetches_published_satisfying_binary`.
- 4.4.9 - Guard set G groups 1, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P5: Write authority and routing
`kind: framing`

**Goal**: one lease registry owns every lease, lock, and write admission; every
writer, including MCP and attention, goes through the coordinator with a typed origin.

### 5.1 Single lease registry owning locks and leases [category: refactor] (depends: 1.3, 3.2)
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py::*` — scope-reason: gains `lock(terminal_id)` refcounted cells and async lease mutations under that lock
- `src/gobby/storage/terminal_settlement.py`
- `tests/storage/test_terminals.py::*` — scope-reason: the machine-scoped orphan sweep cases
- `src/gobby/terminals/write_coordinator.py::*` — scope-reason: `_Lease`, `_lock`, `_locks`, `_lease`, `grant_lease`, and `takeover_lease` are deleted; `__init__`, `lock_held`, `_grant_locked`, `_write_locked`, `_revalidate_lease`, `_persist`, `clear_on_exit`, `write`, `run_sequence`, and `run_native_wake_batch` take the injected registry and its locks; `UnresolvedWriteStore.persist_unresolved_write` gains the required `daemon_epoch`
- `src/gobby/servers/websocket/proxy_relay.py::ProxyHub.finalize_attachment`
- `src/gobby/servers/websocket/proxy_relay.py::ProxyHub._on_socket_fail`
- `src/gobby/servers/websocket/tmux.py::TmuxMixin._cleanup_tmux_client`
- `src/gobby/servers/websocket/tmux.py::TmuxMixin._handle_terminal_attach`
- `src/gobby/servers/websocket/tmux_activation.py::_fail`
- `src/gobby/agents/lifecycle_monitor.py::AgentLifecycleMonitor.__init__`
- `src/gobby/agents/lifecycle_monitor_terminals.py`
- `src/gobby/runner_init/terminal_wiring.py`
- `src/gobby/app_context.py::*` — scope-reason: `ServiceContainer` gains `write_coordinator` and `lease_registry` attributes; its readers are unchanged
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._leases`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._runtime_for`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_take_control`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_terminal_release_control`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._handle_operator_write`
- `src/gobby/servers/websocket/terminal_ws.py::TerminalWsMixin._deliver_operator_write`
- `src/gobby/storage/terminals.py::TerminalManager.persist_unresolved_write`
- `tests/terminals/fakes.py::*` — scope-reason: `MemoryTerminalStore.persist_unresolved_write` stores `daemon_epoch` and the orphan sweep reads it
- `src/gobby/servers/websocket/terminal_ws_control.py`
- `src/gobby/servers/websocket/server.py::*` — scope-reason: the dispatch table binds take and release control from the new mixin and the server no longer constructs a registry
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: registry-injected cases, `attachment_required`, and `runtime_unavailable`
- `tests/terminals/test_write_coordinator.py::*` — scope-reason: revalidate-before-persist and lock ownership
- `tests/terminals/test_lease_authority.py::*` — scope-reason: orphan sweep and refcounted locks
- `tests/terminals/test_write_outcomes.py::*` — scope-reason: `grant_lease` calls become registry `take_control` calls
- `tests/terminals/test_composition_roots.py::*` — scope-reason: one registry instance across container, coordinator, and mixin
- `tests/agents/test_lifecycle_monitor.py::*` — scope-reason: the monitor builds its terminal services through the new factory
- `tests/servers/test_terminal_ws_input.py::*` — scope-reason: input fixtures inject the registry instead of relying on lazy minting
- `tests/servers/test_terminal_ws_resize.py::*` — scope-reason: `_SizingServer` fakes follow the async lease mutations
- `tests/servers/test_terminal_ws_golden.py::*` — scope-reason: take and release control goldens bind the new mixin
- `tests/servers/test_terminal_ws_viewport.py::*` — scope-reason: direct `attach` and `take_control` calls become awaited
- `tests/servers/test_tmux_bridge_authority.py::*` — scope-reason: direct `attach`, `take_control`, and `finalize` calls become awaited
- `tests/servers/test_terminal_list_watermark.py::*` — scope-reason: direct `attach` and `finalize_websocket` calls become awaited
- `tests/terminals/test_native_runtime.py::*` — scope-reason: direct registry `attach` calls become awaited
- `tests/events/test_wake_native_terminal.py::test_latched_wake_is_settled_by_the_delivered_composer_clear`
- `tests/events/test_wake_native_terminal.py::test_undelivered_composer_clear_leaves_the_earlier_wake_latched`
- `src/gobby/communications/native_plan_actions.py::*` — scope-reason: `WriteCoordinator` construction and `run_sequence` call sites take the injected registry
- `src/gobby/terminals/sync_bridge.py::*` — scope-reason: `WriteCoordinator` construction and `write` call sites take the injected registry
- `src/gobby/terminals/services.py::*` — scope-reason: `write` and `run_sequence` call sites re-verified against the lock-owning registry; no change expected
- `src/gobby/agents/runner.py::*` — scope-reason: `AgentLifecycleMonitor` construction re-verified; parameters unchanged
- `src/gobby/runner.py::*` — scope-reason: `AgentLifecycleMonitor` and `WriteCoordinator` construction re-verified against the injected registry
- `tests/communications/test_native_plan_actions.py::*` — scope-reason: coordinator fixtures inject the registry
- `tests/mcp_proxy/test_registries.py::*` — scope-reason: coordinator fixtures inject the registry
- `tests/test_runner_init.py::*` — scope-reason: coordinator fixtures inject the registry
- `tests/agents/watchdog/test_close_review_parked_caller.py::*` — scope-reason: `AgentLifecycleMonitor` fixtures re-verified; parameters unchanged
- `tests/e2e/test_build_dispatcher_autonomy.py::*` — scope-reason: `AgentLifecycleMonitor` fixtures re-verified; parameters unchanged
- `tests/agents/test_terminal_prompt_monitor.py::*` — scope-reason: `write` fixtures re-verified against the lock-owning registry
- `tests/terminals/test_sync_bridge.py::*` — scope-reason: coordinator fixtures inject the registry
- `tests/terminals/test_write_input.py::*` — scope-reason: `write` fixtures re-verified against the lock-owning registry

`terminal_wiring.py` constructs one `TerminalLeaseRegistry`, passes it to
`WriteCoordinator(manager, runtime_registry, lease_registry=…)`, stores both on
`ServiceContainer.write_coordinator` and `ServiceContainer.lease_registry`, and hands
them to the WebSocket server. The coordinator's private lease table, `_Lease`,
`grant_lease`, `takeover_lease`, `_lock`, `_lease`, and `_locks` are deleted;
`_persist` runs after `_revalidate_lease`. Per-terminal locks live on the registry as
refcounted cells. Every mutation of attachment or writer authority (`attach`,
`take_control`, `release_control`, `finalize`, `finalize_websocket`, and the exit
cleanup in `WriteCoordinator.clear_on_exit`) becomes `async`, acquires the registry's
refcounted per-terminal lock, and is awaited by every production caller
(`TmuxMixin._handle_terminal_attach`, `TmuxMixin._cleanup_tmux_client`,
`ProxyHub.finalize_attachment`, `ProxyHub._on_socket_fail`, `tmux_activation._fail`)
and every direct test caller. `ProxyHub.finalize_attachment` removes the relay record
synchronously, awaits registry finalization immediately to revoke write authority, and
only then cancels the pump and awaits frame close; `_on_socket_fail` uses the same
authority-before-transport-teardown order. Finalization never removes a lock cell while
an owner or waiter still borrows it, and finalize followed by re-attach reuses one cell.
Every operator write (`input`, `paste`, `text`) is delivered by
`_deliver_operator_write` through `WriteCoordinator.write` with `origin="operator"`;
the landed branch that calls `runtime.write_input`, `write_paste`, or `write_text`
directly whenever a runtime resolves is deleted, so the coordinator is the only
dispatcher. The coordinator resolves the runtime from the row's backend and answers
`runtime_unavailable` when none resolves, which keeps the typed handling of 5.1.5 and
makes takeover, release, finalize, and exit cleanup linearize with web writes exactly as
5.1.9 promises. The startup orphan sweep is
`TerminalManager.clear_orphaned_attachment_writes(machine_id, daemon_epoch)` in
`terminal_settlement.py`, because the persisted unresolved-write latch is the only
durable carrier of in-flight `ws:` entries and a fresh in-memory registry cannot see
them. The latch entry today holds only `at` and `origin`, and the `ws:<attachment>:<seq>`
key carries no epoch, so `persist_unresolved_write` gains a required `daemon_epoch`
argument stored beside `at` and `origin`; the coordinator receives the daemon epoch at
construction from `terminal_wiring.py` and passes it on every write-ahead latch. Move
the unresolved-write latch members (`persist_unresolved_write` and its resolve and
clear companions) out of `terminals.py` into `terminal_settlement.py` next to 1.3's
settlement CAS members while adding that argument, so `terminals.py` stays below 800
lines and 5.2 edits the latch in its final home.
`terminal_wiring.py` calls the sweep exactly once, after the manager exists and before
the registry or coordinator accepts any write; it deletes every `ws:` latch on this
machine's rows whose stored `daemon_epoch` differs from the current one, and leaves
other machines' rows and the current epoch's entries alone. Move the terminal-services
construction out of `AgentLifecycleMonitor.__init__` into
`build_terminal_services(db, tmux, lease_registry)` in the new
`lifecycle_monitor_terminals.py` so `lifecycle_monitor.py` drops below 950 lines.
Move `_handle_terminal_take_control` and `_handle_terminal_release_control` out of
`terminal_ws.py` (870 lines today) into a `TerminalControlMixin` in the new
`terminal_ws_control.py`, so `terminal_ws.py` shrinks rather than grows. `_leases()` no longer
mints; a missing registry raises at startup. `_runtime_for`'s `unittest.mock`
fall-through is deleted; an unresolvable runtime answers `runtime_unavailable`, and an
operator write without `attachment_id` answers `attachment_required`.

**Acceptance:**

- 5.1.1 - The container, coordinator, and WebSocket mixin share one `TerminalLeaseRegistry` instance, and `WriteCoordinator` has no `_leases`, `grant_lease`, `takeover_lease`, or `_locks`. test: `tests/terminals/test_composition_roots.py::test_single_lease_registry_is_injected`.
- 5.1.2 - A write whose lease was taken over between admission and persist is refused `lease_lost` and nothing is persisted. test: `tests/terminals/test_write_coordinator.py::test_revalidate_before_persist`.
- 5.1.3 - Concurrent `take_control` calls on one terminal serialize under `registry.lock(terminal_id)` and the cell is released when the last holder exits. test: `tests/terminals/test_lease_authority.py::test_lock_cells_are_refcounted`.
- 5.1.4 - `TerminalManager.clear_orphaned_attachment_writes(machine_id, daemon_epoch)` deletes persisted `ws:` latches whose stored `daemon_epoch` is an earlier epoch on this machine's rows, leaves the current epoch's and other machines' entries, and does so after a real store round trip through PostgreSQL and `MemoryTerminalStore` with the entries written by `persist_unresolved_write`. test: `tests/storage/test_terminals.py::test_orphan_sweep_clears_only_dead_epochs_on_this_machine`.
- 5.1.12 - `persist_unresolved_write` stores `daemon_epoch` on every entry beside `at` and `origin`, raises `ValueError` without it, and the coordinator passes the epoch it was constructed with on every operator latch. test: `tests/storage/test_terminals.py::test_latch_entries_carry_daemon_epoch`, `tests/terminals/test_write_coordinator.py::test_operator_latch_carries_daemon_epoch`.
- 5.1.13 - With a resolved runtime paused mid-dispatch, an `input`, `paste`, and `text` operator write each reach the runtime only through `WriteCoordinator.write`, a takeover, release, finalize, or exit cleanup issued meanwhile waits behind the write, and `terminal_ws.py` contains no direct `write_input`, `write_paste`, or `write_text` call. test: `tests/servers/test_terminal_ws_input.py::test_operator_writes_route_through_coordinator`.
- 5.1.10 - `terminal_wiring.py` runs the orphan sweep once, after the manager is built and before the registry or coordinator is handed to any consumer, and constructs exactly one registry. test: `tests/terminals/test_composition_roots.py::test_wiring_sweeps_orphans_before_accepting_writes`.
- 5.1.5 - An operator write without `attachment_id` is answered `attachment_required`; an unresolvable runtime is answered `runtime_unavailable`; `terminal_ws.py` imports nothing from `unittest.mock`. test: `tests/servers/test_terminal_ws_lease.py::test_attachment_required_and_runtime_unavailable`.
- 5.1.6 - `lifecycle_monitor.py` and `terminal_ws.py` are each under 950 lines, the monitor builds its terminal services through `build_terminal_services`, and take and release control are served from `TerminalControlMixin`. symbol: `build_terminal_services`. file: `src/gobby/agents/lifecycle_monitor_terminals.py`. file: `src/gobby/servers/websocket/terminal_ws_control.py`.
- 5.1.7 - Every production and direct-test caller awaits the async lease mutations, and the focused suites emit no un-awaited-coroutine warning. test: `tests/servers/test_tmux_bridge_authority.py::test_async_lease_mutation_callers_are_awaited`.
- 5.1.8 - With `frame.close()` paused, an input racing either proxy-relay cleanup path is refused stale and reaches no runtime; both paths revoke authority before frame teardown. test: `tests/servers/test_terminal_ws_lease.py::test_finalize_revokes_authority_before_frame_close`.
- 5.1.9 - Takeover, release, finalize, and exit cleanup each wait behind an in-flight write and preserve its latch until settlement; finalize followed by re-attach reuses one lock cell, and cancelling a queued waiter releases only its borrow. test: `tests/terminals/test_write_coordinator.py::test_lease_mutations_linearize_with_dispatch`.
- 5.1.11 - Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 5.2 `send_keys` and attention respond through the coordinator [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::send_keys`
- `src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py`
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py::register_terminal_tools`
- `src/gobby/terminals/write_coordinator.py::*` — scope-reason: `WriteRequest` gains the optional `idempotency_key`; `write` and `_write_locked` latch the key with a payload fingerprint; `UnresolvedWriteStore.persist_unresolved_write` carries the optional fingerprint
- `src/gobby/servers/routes/attention.py::respond`
- `src/gobby/storage/terminals.py::TerminalManager.persist_unresolved_write`
- `src/gobby/storage/terminal_settlement.py`
- `tests/terminals/fakes.py::*` — scope-reason: `MemoryTerminalStore.persist_unresolved_write` carries the optional payload fingerprint
- `tests/storage/test_terminals.py::*` — scope-reason: fingerprint round-trip cases for the unresolved-write latch
- `tests/mcp_proxy/test_sessions_terminal_tools.py::*` — scope-reason: origin and idempotency cases
- `tests/servers/test_attention_respond.py::*` — scope-reason: coordinator routing with precondition CAS
- `tests/servers/test_attention_roster.py::*` — scope-reason: roster cases construct the route with a coordinator
- `tests/mcp_proxy/tools/sessions/test_terminal.py::*` — scope-reason: `send_keys` imports from its new module
- `src/gobby/mcp_proxy/tools/sessions/_factory.py::*` — scope-reason: `register_terminal_tools` and `send_keys` are imported from the new module
- `src/gobby/communications/native_plan_actions.py::*` — scope-reason: `WriteRequest` construction re-verified; `idempotency_key` is optional
- `src/gobby/terminals/services.py::*` — scope-reason: `WriteRequest` and `write` call sites re-verified; `idempotency_key` is optional
- `src/gobby/terminals/sync_bridge.py::*` — scope-reason: `WriteRequest` and `write` call sites re-verified; `idempotency_key` is optional
- `tests/agents/test_terminal_prompt_monitor.py::*` — scope-reason: `write` fixtures re-verified; `idempotency_key` is optional
- `tests/terminals/test_sync_bridge.py::*` — scope-reason: `WriteRequest` fixtures re-verified; `idempotency_key` is optional
- `tests/terminals/test_write_input.py::*` — scope-reason: `WriteRequest` fixtures re-verified; `idempotency_key` is optional

Move `send_keys` out of `_terminal.py` into the new
`src/gobby/mcp_proxy/tools/sessions/_terminal_send_keys.py` (883 lines today; the
move keeps `_terminal.py` under 800); `register_terminal_tools` imports it from there.
`send_keys` submits with `origin="daemon"` and gains an optional caller-supplied
`idempotency_key` (1 to 128 characters from `[A-Za-z0-9._:-]`); when absent it mints a
fresh key and returns it. The action key is `mcp-send-keys:{session_id}:{idempotency_key}`,
carried on `WriteRequest`. `_write_locked` reuses the existing unresolved-write latch
(`TerminalManager.persist_unresolved_write`) as the only carrier: it persists the
payload fingerprint when an outcome is indeterminate, returns the stored indeterminate
outcome for a same-key same-payload retry without a second write (no runtime
acknowledgement can prove that arbitrary text, named keys, or control input reached the
PTY, so the latch is never resolved from capture and the caller decides whether to
send again under a new key), refuses a different payload as `idempotency_conflict`, and
clears the latch on delivered or refused so a later call is a fresh write. No separate
idempotency table or result cache exists. 5.1's move of the unresolved-write latch
members (`persist_unresolved_write` and its resolve and clear companions) out of
`terminals.py` into `terminal_settlement.py` means the fingerprint fields are added
there; `terminals.py` stays below 800 lines. Attention `respond` submits through the coordinator with
`origin="attention"` under the existing `_TrackedEntryLock` and a precondition CAS on
the attention entry's state, so a stale respond never writes.

**Acceptance:**

- 5.2.1 - `send_keys` against a terminal with a live operator lease is delivered with `origin="daemon"` instead of refused stale, and a same-key replay returns the recorded outcome. test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_keys_uses_daemon_origin_and_idempotency`.
- 5.2.2 - A same-key replay with a different payload fingerprint is refused `idempotency_conflict`. test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_keys_idempotency_conflict`.
- 5.2.3 - Attention `respond` writes through the coordinator with `origin="attention"` and refuses when the entry's state changed after the caller read it. test: `tests/servers/test_attention_respond.py::test_respond_routes_through_coordinator_with_cas`.
- 5.2.4 - An explicit-key retry of an unresolved write dispatches nothing to the runtime and returns the stored indeterminate outcome with the same key; reuse of the key after a delivered outcome is a fresh write; a malformed key is a typed refusal; the no-key form returns a fresh key whose retry behaves the same way. test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_keys_idempotency_key_contract`.
- 5.2.5 - Payload fingerprints round-trip through PostgreSQL and `MemoryTerminalStore` and count against the existing unresolved-write entry and byte caps. test: `tests/storage/test_terminals.py::test_unresolved_write_fingerprint_round_trip`.
- 5.2.6 - Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P6: Web input path
`kind: framing`

**Goal**: the browser terminal takes and releases control honestly, settles every
write, survives lease loss read-only, and renders frames and history within bounds.

### 6.1 Control lease lifecycle and write settlement in the web client [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `web/src/hooks/useTmuxSessions.ts::*` — scope-reason: gains `takeControl`, `releaseControl`, and a single `pendingInput` slot; roster and settlement logic move out to keep it under 800 lines
- `web/src/hooks/terminalWriteSettlement.ts`
- `web/src/hooks/terminalRosterSnapshot.ts`
- `web/src/components/activity/terminal/TerminalView.tsx::*` — scope-reason: focus, blur, and first-keystroke lease hooks, paste, and the read-only overlay on lease loss
- `web/src/components/activity/terminal/TerminalTab.tsx::*` — scope-reason: stable mount keyed by `terminal_id`
- `web/src/hooks/__tests__/terminalWriteSettlement.test.ts`
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: lease and settlement cases
- `web/src/components/activity/terminal/__tests__/TerminalView.test.tsx::*` — scope-reason: read-only overlay and paste cases
- `web/tests/terminal-control-lease.spec.ts`

The client sends `terminal_take_control` on focus or first keystroke, holding the
triggering keystroke as the single `pendingInput` until `control_result`; further
keystrokes while the slot is occupied are refused with a visible "waiting for control"
cue and never queued. The slot is delivered exactly once under the installed generation
on `granted:true`, and discarded with a visible refusal on `granted:false`, request
timeout, `lease_lost`, blur, or reconnect; each of those also clears any in-flight
control request so a stale `control_result` cannot grant a later generation.
`terminal_release_control` is sent on blur. Take-back, retry, discard, and
jump-to-bottom use the shared button controls, are keyboard operable with visible focus
and non-colour state cues, and meet the 44×44 coarse-pointer target. Move the
write-outcome state into the new `terminalWriteSettlement.ts` reducer keyed by
attachment and `client_write_seq`: `delivered` clears, `refused` discards with a
toast, `indeterminate` offers retry-or-discard once and never auto-resends. Move the
roster derivation into the new `terminalRosterSnapshot.ts`. `lease_lost` puts the view
read-only until the next take. Paste sends `terminal_paste`. The terminal component
mounts once per `terminal_id` and survives roster reorders.

**Acceptance:**

- 6.1.1 - Focusing a terminal sends one `terminal_take_control`, a keystroke before `control_result` is held in the single pending slot and sent after, and blur sends `terminal_release_control`. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::takes and releases control around focus`.
- 6.1.2 - The reducer clears on `delivered`, discards on `refused`, and on `indeterminate` exposes exactly one retry that resends the same seq and payload. test: `web/src/hooks/__tests__/terminalWriteSettlement.test.ts::settles outcomes without auto-resend`.
- 6.1.3 - After `lease_lost` the view is read-only and typing does not send input until control is retaken; paste sends `terminal_paste` with the clipboard text. test: `web/tests/terminal-control-lease.spec.ts::lease loss goes read-only and paste is bracketed`.
- 6.1.4 - Reordering the roster does not remount the terminal component for an unchanged `terminal_id`. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::keeps terminal mounts stable across roster changes`.
- 6.1.5 - A second keystroke while `pendingInput` is occupied is refused with the visible cue and never sent; `granted:false`, request timeout, `lease_lost`, blur, and reconnect each discard the slot with a visible refusal and clear the in-flight control request, and a `control_result` arriving after that clear grants nothing. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::refuses extra input and clears the pending slot on every failure path`.
- 6.1.6 - Take-back, retry, discard, and jump-to-bottom are reachable by keyboard with visible focus, expose non-colour state cues, and measure at least 44×44 at the 440×956 tier. test: `web/tests/terminal-control-lease.spec.ts::lease controls are keyboard operable and sized for touch`.
- 6.1.7 - Guard set G group 6 passes from the `0.5.0` checkout, plus `npx playwright test tests/terminal-control-lease.spec.ts` at the three tiers. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 6.2 Fragment accumulation, readiness rendezvous, and bounded history [category: code] (depends: 6.1)
`kind: deliverable`

Targets:
- `web/src/hooks/terminalWsFragments.ts::*` — scope-reason: fragment accumulation with a tick and a per-fragment overhead charge
- `web/src/hooks/terminalAttachmentReadiness.ts`
- `web/src/hooks/useTmuxSessions.ts::*` — scope-reason: `refresh` with `kind:"refresh"` and snapshot pinning, the `setViewport` readiness rendezvous, and bounded per-attachment history replace; readiness state moves out to stay under 800 lines
- `web/src/components/activity/terminal/TerminalView.tsx::*` — scope-reason: history replace on attachment change
- `web/src/hooks/__tests__/terminalWsFragments.test.ts`
- `web/src/hooks/__tests__/terminalAttachmentReadiness.test.ts`
- `web/tests/terminal-history-scroll.spec.ts::*` — scope-reason: bounded-history cases at the three viewports

Fragments accumulate per attachment and flush on a tick, charging 256 B overhead per
fragment against a 256 KiB budget; over budget the client requests
`{kind:"refresh"}` and pins the next snapshot. Move the attachment readiness state
out of `useTmuxSessions.ts` into the new `terminalAttachmentReadiness.ts`, which keeps
`useTmuxSessions.ts` under 800 lines; `setViewport` is sent only after the attachment
is ready. History is replaced per attachment, pinned at 500 lines and
bounded at 2 000 lines or 256 KiB. A replacement attachment installed by a reconnect
resets the mounted renderer's buffer before applying its bounded history: the same
component instance is retained, each history line renders once, and the scroll
position derives from the new window. At the 440×956 and 932×430 tiers the history
scrolls by touch and live output does not snap the view back to the bottom while the
user is scrolled up.

**Acceptance:**

- 6.2.1 - Fragments flush once per tick and exceeding the 256 KiB budget (256 B overhead per fragment) triggers exactly one `refresh` whose snapshot is pinned. test: `web/src/hooks/__tests__/terminalWsFragments.test.ts::charges overhead and refreshes over budget`.
- 6.2.2 - `setViewport` is not sent before the attachment reports ready and is sent once after. test: `web/src/hooks/__tests__/terminalAttachmentReadiness.test.ts::defers viewport until ready`.
- 6.2.3 - History is replaced when the attachment changes and never exceeds 2 000 lines or 256 KiB at 440×956, 932×430, and 1440×900; at the two mobile tiers a line above the initial viewport becomes visible by touch scroll and live output does not snap the view back. test: `web/tests/terminal-history-scroll.spec.ts::bounds history per attachment across tiers`.
- 6.2.4 - A reconnect that installs a replacement attachment whose history overlaps the rendered tail keeps the same component instance, renders each numbered line exactly once in order, and derives the scroll position from the new window. test: `web/tests/terminal-history-scroll.spec.ts::replacement attachment resets history without remount`.
- 6.2.5 - Guard set G group 6 passes from the `0.5.0` checkout, plus `npx playwright test tests/terminal-history-scroll.spec.ts` at the three tiers. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P7: Host backpressure and acceptance
`kind: framing`

**Goal**: a slow observer can never stall the host or other observers, and the
native runtime's acceptance is proven by real host-driven tests.

### 7.1 Host backpressure module [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gterminal/src/host/backpressure.rs`
- `crates/gterminal/src/host/backpressure/tests.rs`
- `crates/gterminal/src/host/native_ops.rs`
- `crates/gterminal/src/host/control.rs::handle_connection`
- `crates/gterminal/src/host/events.rs`
- `crates/gterminal/src/host/config.rs::HostConfig`
- `crates/gterminal/src/host/config.rs::HostConfig::validate`
- `crates/gterminal/src/host/config.rs::HostConfig::default`
- `crates/gterminal/tests/frame_producer.rs::*` — scope-reason: lagged-close and keyframe cases
- `crates/gterminal/src/host/write.rs::*` — scope-reason: the `HostConfig::default` call site re-verified; the backpressure caps carry defaults

`backpressure.rs` owns per-observer `queued_bytes`; over `delta_queue_bytes` the
producer in `native_ops.rs` drops queued deltas and enqueues a keyframe; an observer
that has not drained for `lag_timeout` (5 s) is closed with `lagged`. Control responses
queue with a 2 s `control_deadline`; events past the event queue cap end the
subscription with `event_overflow`. Caps are `HostConfig` fields with the defaults in
the Constraints and are host-internal. Tests run with small test-local caps and
deadlines and assert that the shipped defaults remain 5 s and 2 s, so no test sleeps
those durations.

**Acceptance:**

- 7.1.1 - A frame peer that exceeds `delta_queue_bytes` but drains before `lag_timeout` never holds more than the byte cap, receives exactly one replacement keyframe, stays connected, and does not slow a continuously reading observer. test: `crates/gterminal/tests/frame_producer.rs::slow_observer_resyncs_with_one_keyframe`.
- 7.1.2 - A frame peer that never drains is closed `lagged` at the configured timeout and its observer slot is released, while another observer keeps receiving frames. test: `crates/gterminal/tests/frame_producer.rs::lagged_observer_is_closed_and_released`.
- 7.1.3 - Control responses are not coalesced below the queue cap; a non-reading control peer is closed at `control_deadline`, an overflowing event subscriber receives `event_overflow`, and the shipped defaults are 5 s and 2 s. test: `crates/gterminal/src/host/backpressure/tests.rs::control_deadline_and_event_overflow`.
- 7.1.4 - Guard set G groups 3 and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

### 7.2 Host-driven acceptance tests [category: test] (depends: 3.2, 5.2, 7.1)
`kind: deliverable`

Targets:
- `tests/terminals/acceptance/__init__.py`
- `tests/terminals/acceptance/conftest.py`
- `tests/terminals/acceptance/test_native_lifecycle.py`
- `tests/terminals/acceptance/test_native_writes.py`
- `tests/terminals/acceptance/test_tmux_parity.py`
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: the single live-host capacity scenario at the shipped admission ceilings
- `.github/workflows/ci.yml`

Acceptance runs against a real `gterm host` built from the tree on a 120×40 PTY:
spawn, commit, and exit settlement; exec failure; host crash and respawn; drain; lease
takeover and write settlement; `send_keys` idempotency; and a tmux parity list
(attach, resize, write, exit) mirrored across both backends. Each module's header
table maps every test to the item of this plan it proves; no case count is asserted.
One Rust live-host capacity test lists the maximum admissible population, 124 native
terminals (`max_attachments_total` 128 less the four reserved lifecycle slots) plus 64
tmux observers (`max_attached_terminals`), each with a 1 024-byte title and
maximum-size bounded fields, proves the `list` envelope stays under the control line
cap, and shows the 189th admission refused `capacity`; those numbers are the
population of that one scenario, never a number of tests. Add a `terminal-acceptance`
job to `ci.yml` that builds the host and runs the suite on macOS and Linux.

**Acceptance:**

- 7.2.1 - The acceptance package covers spawn, commit, and exit settlement, exec failure, host crash and respawn, drain, lease takeover and write settlement, `send_keys` idempotency, and the tmux parity list, with a header table in each module mapping every test to the plan item it proves. file: `tests/terminals/acceptance/test_native_lifecycle.py`. file: `tests/terminals/acceptance/test_native_writes.py`. file: `tests/terminals/acceptance/test_tmux_parity.py`.
- 7.2.2 - The suite passes against a freshly built host on macOS and Linux in the `terminal-acceptance` job. file: `.github/workflows/ci.yml`.
- 7.2.3 - `list` at the shipped ceilings (124 native terminals plus 64 tmux observers, each with a 1 024-byte title and maximum-size bounded fields) from a live host stays under the control line cap, and the 189th admission is refused `capacity`. test: `crates/gterminal/tests/control_protocol.rs::list_envelope_fits_under_line_cap`.
- 7.2.4 - Guard set G groups 2, 3, and 7 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out, plus `GOBBY_RUN_VENDOR_BUILD=1 uv run pytest tests/terminals/acceptance`. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## P8: Flip evidence
`kind: framing`

**Goal**: the flip decision has one honest, local, checkable input.

### 8.1 Flip evidence document and checker [category: test]
`kind: deliverable`

Targets:
- `docs/evidence/native-backend-flip.md`
- `tests/config/test_native_backend_flip.py`
- `docs/guides/gterminal-development-guide.md`

`docs/evidence/native-backend-flip.md` starts as an honest stub ("no qualifying run")
and gains one row per qualifying CI run (date, OS, commit, workflow run URL, result).
`check_native_backend_flip(evidence_text, default_backend)`, a helper local to the
test module, passes only when `default_backend == "tmux"`, or when the evidence has a
green row for each of macOS and Linux at the same commit and no red row after it. The
dev guide's "Backend status" section documents the gate and the rollback
(`gobby config set terminals.default_backend tmux`). This leaf lands the stub, checker,
and documentation independently of P7; only adding a qualifying evidence row and
dispatching D1 wait for a green P7 macOS and Linux run at one commit.

**Acceptance:**

- 8.1.1 - With `default_backend="tmux"` the checker passes on the stub; with `"native"` it fails on the stub, fails when only one OS is green, and passes with both OSes green at one commit. test: `tests/config/test_native_backend_flip.py::test_flip_gate_rules`.
- 8.1.2 - The checked-in default is `tmux`, the evidence document exists, and the dev guide's "Backend status" section names the gate and the rollback. symbol: `TerminalConfig`. file: `docs/evidence/native-backend-flip.md`. behavior: "Backend status" in `docs/guides/gterminal-development-guide.md`.
- 8.1.3 - A later red row after a qualifying same-commit macOS/Linux green pair causes the checker to fail under the documented row ordering. test: `tests/config/test_native_backend_flip.py::test_flip_gate_rules`.
- 8.1.4 - Guard set G groups 1 and 5 pass from the `0.5.0` checkout with the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.

## D1 Native default flip (depends: 8.1)
`kind: deferred`

Flipping `TerminalConfig.default_backend` to `native` (original QA items 5.3.1 and
5.3.2) waits on 8.1's gate: green P7 acceptance on macOS and Linux at one commit,
recorded in `docs/evidence/native-backend-flip.md`. It is a one-line config change
plus the evidence row and a ROADMAP note, dispatched as its own task when the gate
passes.

```yaml
deferral:
  task_ref: "#22104"
  reason: "The flip is gated on acceptance evidence that can only exist after P7 lands and runs on both operating systems; it must not be dispatched from this plan's manifest."
  owner: "backend-developer"
  original_acceptance_items:
    - 5.3.1
    - 5.3.2
```

## V1 Plan Changelog
`kind: verification`

**Draft** (2026-09-09)

- source: `.gobby/plans/herdr-terminal-client-qa-fixes.md` sections 2.3–2.9, 3.1–3.4,
  4.1, 4.2, 4.6, 5.1, 5.2, 8.1, 8.2, and D1; `.gobby/plans/herdr-client-completion.md`
  D1; `ROADMAP.md` Stage 0.
- Decision Record confirmed in conversation on 2026-09-09 (see Constraints).
- Verification commands for every leaf: Guard set G groups 1–6 from
  `docs/guides/gterminal-development-guide.md` with `DATABASE_URL` on the isolated test
  hub and `GOBBY_TEST_PROTECT=1`; Rust
  `cargo build --release -p gobby-terminal --features vt-engine --bin gterm`,
  `cargo clippy -p gobby-terminal -p gobby-client --all-targets -- -D warnings`, and
  `cargo nextest run -p gobby-terminal --features vt-engine`; web
  `cd web && npx vitest run src/hooks src/components/activity` and
  `npx playwright test tests/terminal-control-lease.spec.ts tests/terminal-history-scroll.spec.ts`;
  end to end after P7 `GOBBY_RUN_VENDOR_BUILD=1 uv run pytest tests/terminals/acceptance`
  locally, then the `terminal-acceptance` CI job on both operating systems.
- Plan validation: `uv run gobby plans validate .gobby/plans/native-runtime-completion.md -p /Users/josh/Projects/gobby`.

**Round 1** `kind: enhancement`

- enhancer_run: 759bac80-6a90-4377-929c-fd057237473f (relaunch of run 0052ac86-ba9a-4e43-a00e-6545f1f30a2c, which was cancelled during skill loading with no output)
- enhancer_session: 23da8f79-87f3-48dd-bce5-2afad20c7e81
- converged: false
- suggestions_presented: 8
- accepted:
  - E1 / better / 5.1 names the async lease-mutation callers, the authority-before-frame-teardown order, and items 5.1.7–5.1.9 (5.1.9 trimmed to the cell-reuse and waiter-cancellation clauses)
  - E2 / better / 1.2 shields shared restart waiters and fences restart creation on stop; items 1.2.6–1.2.7
  - E4 / better / 5.2 defines the `send_keys` idempotency key origin, lifetime, and carrier through the existing unresolved-write latch; items 5.2.4–5.2.5 (legacy-entry compatibility clause dropped under the no-backward-compatibility rule)
  - E5 / better / 3.2 defines the gap-recovery cut under event churn and reader-generation teardown; items 3.2.8–3.2.10
  - E6 / better / one acceptance item per table data row: 1.2.8–1.2.14 for the outcome table, 4.4.5 narrowed and 4.4.6 added for the installer table (required by `docs/contracts/plan-coverage.md` Table-Row Decomposition)
  - E7 / better / 7.1.1 split into deterministic resync, lagged-close, and control/event items 7.1.1–7.1.3 with test-local caps
  - E8 / better / 8.1 no longer depends on 7.2; the stub, checker, and docs land independently of the P7 run
- declined:
  - E3 / better / `settle_lock` spans for the reaper, retry, and list reconciliation plus items 1.3.6–1.3.8: over-mechanism at rung 1, because the attempt-generation CAS and kill-by-captured-identity in 1.3 already prevent an old attempt from failing or killing a replacement; only the one-sentence web timeout-callback clarification was kept in 1.3 and 1.3.2
- resolution_notes: Seven suggestions folded into 1.2, 1.3, 3.2, 4.4, 5.1, 5.2, 7.1, and 8.1 as listed; every vote walked the restraint ladder and the user confirmed the votes as proposed. Constraints 1–11 unchanged.

**Round 1** `kind: verification`

- reviewer_run: 3aba1b50-e4c7-43cf-a892-35b76821549d
- reviewer_session: c62b1ffa-fee6-4802-bd2b-3de410dd6fb5
- verdict: needs_review
- findings:
- PR1-response-builder-name / blocking / 1.1 names both `_build_spawn_success_response` and `build_spawn_response` — accepted; `build_spawn_response` is the one name
- PR1-spawn-outcome-closure / blocking / 1.2 outcome table misses stale, not_native, epoch-change, cancellation, unreachable-host reaper, and duplicate-session branches — accepted (modified); rows and items added for the branches that are real gaps in the landed tree, late prepare already covered by 1.3
- PR1-host-safety-closure / blocking / 2.1 lacks the full non-native guard matrix, UTF-8 snapshot truncation, and typed observer close — accepted (modified); idempotent shutdown is landed and restated in 3.1, the capped `read_until` reader is already specified
- PR1-gate-preexec-closure / blocking / 2.2 bare EOF on the status pipe cannot distinguish exec from pre-exec death — accepted; `waitpid` disambiguation, errno variants, `stage` in the status, residue assertions
- PR1-control-recovery-closure / blocking / 3.2 invalid `readline(limit=)`, host-side head-of-line blocking, unbounded gap buffer, missing listed-prepared, large-line, and tmux-event branches — accepted (modified); superseded-attempt is already settled by 1.3's attempt CAS
- PR1-packaging-parity / blocking / 4.1 omits package-list parity, NOTICE, no-Zig default, Windows cross-lint, inventory uniqueness — accepted (modified); no-Zig gate, Windows cross-lint, and NOTICE.md are landed in `rust-ci.yml` and the crate and are cited; package-list parity and license-warning items added; inventory-uniqueness guard declined at rung 1
- PR1-installer-state-closure / blocking / 4.4 table omits the existing `force` dimension and the source-build paths — accepted
- PR1-orphan-sweep-carrier / blocking / 5.1 assigns the startup orphan sweep to the in-memory registry — accepted; sweep moves to `terminal_settlement.py`, called from `terminal_wiring.py`
- PR1-send-keys-indeterminate / blocking / 5.2 capture cannot prove delivery of an indeterminate write — accepted; same-key retry returns the stored indeterminate outcome without redispatch
- PR1-web-input-closure / blocking / 6.1 later keystrokes, cleanup, accessibility, touch scrolling, and replacement attachment undefined — accepted (modified); fragments, readiness, and history bounds already live in 6.2
- PR1-capacity-test-misread / blocking / 7.2 misreads the 124 + 64 capacity population as 188 test cases — accepted (modified); the seventeen named Rust tests exist only in the unlanded worktree draft, one live-host capacity test is specified instead
- PR1-flip-red-case / blocking / 8.1 lacks the later-red-row negative case — accepted (typed repair)
- PR1-guard-g-propagation / blocking / Guard set G never reaches leaf validation criteria — accepted; one close-gate line per deliverable
- resolution_notes: The source QA plan was written against a failed worktree build and never expanded (#21334 came from `herdr-client-completion`), so parity with it is not a defect; every finding was judged against the landed tree and the plan. All thirteen findings accepted, six with the worktree-only parts declined. Round cap is 1; the repaired artifact goes to human handoff.

```json plan-review-round
{"evidence_id":"e9a4f711-e6e2-4f39-8ed5-f12467987346","plan_hash":"3cb55044aca7abbbdd64046e5889e3b81259f5bbaceccf77b7e1326db09a23af","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"0afe265b989a0108d7a4f9c28aedabb75cdbe0f33ddc2c73f53a998227651b07","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":9,"emitted_findings":13,"total":22},"evidence_id":"e9a4f711-e6e2-4f39-8ed5-f12467987346","lanes":[{"candidate_count":12,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":10,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"3d9fc833f12ca29f4ca407acb28060fed1c04ba5eedf5272e5374837d376834a","status":"valid"},"source_digest":"ef6c82ef1d4ad293c6fc947baba6c99c50065e078b396b7e5451c98f7259fb67","version":1},"findings":[{"category":"traceability","check_key":"response-builder-contract","description":"Section 1.1 specifies two different response-builder names, leaving the implementation and verification contract ambiguous.","finding_id":"PR1-response-builder-name","fix":"Choose one canonical builder name and use it consistently in the implementation prose, exact target, caller import, and acceptance symbol.","location":"Implementation paragraph and Acceptance symbols","prevention":"Cross-check every moved or introduced symbol against its target entry, caller import, and acceptance artifact.","principle":"A deliverable must name one exact symbol consistently across implementation, targets, callers, and acceptance.","root_cause":"The extraction prose names `_build_spawn_success_response`, while acceptance names `build_spawn_response`.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"spawn-outcome-branch-closure","description":"The spawn lifecycle is not closed over the source plan's refusal, cancellation, late-prepare, retry, cleanup, and reaper outcomes; the transport boundary also leaves drain and cancellation-after-write settlement undefined.","finding_id":"PR1-spawn-outcome-closure","fix":"Restore explicit behavior and focused acceptance for stale/not_native/epoch-change refusals, cancellation before and after commit write, late prepare, retry/duplicate cleanup, unreachable-host reaping, and pre-write versus post-write/drain/read/deadline indeterminate settlement, or cite concrete landed evidence for each omitted branch.","location":"Sections 1.2–1.3 implementation and acceptance","prevention":"Diff replacement acceptance branch-by-branch against the governing source plan and record an implementation, test, or landed-evidence disposition for each branch.","principle":"A replacement plan must preserve every still-owned failure and cancellation outcome or cite concrete evidence that it already landed.","root_cause":"The source QA 2.4–2.5 matrix was compressed without dispositioning stale, not-native, epoch-change, cancellation, late-prepare, retry, duplicate-cleanup, and unreachable-host reaper branches.","section_id":"1.2","severity":"blocking"},{"category":"missing-requirement","check_key":"native-operation-guard-matrix","description":"The plan does not prove that kill, write, paste, resize, and snapshot paths all reject non-native targets before mutation; it also omits UTF-8-safe snapshot truncation, observer close behavior, idempotent shutdown, and an allocation-bounded no-newline read.","finding_id":"PR1-host-safety-closure","fix":"Restore the complete guard matrix and acceptance, specify an allocation-bounded incremental Rust reader for peers that never send a newline, test UTF-8 truncation, and define typed observer close plus repeatable shutdown behavior.","location":"Sections 2.1 and 3.1 native host safety contract","prevention":"Enumerate every native-only verb and every bounded-read/lifecycle edge, then attach a focused acceptance case to each.","principle":"Every native-only mutation must be rejected before state change, and bounded I/O and lifecycle closure must be executable and testable.","root_cause":"The QA 2.6/2.8 replacement omitted several native-only guards, UTF-8 truncation boundaries, typed observer closure, and shutdown idempotence.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"preexec-status-closure","description":"The exec barrier lacks coverage for child or host death before exec, errno variants, injected setup failures, bare EOF, and daemon-side final settlement.","finding_id":"PR1-gate-preexec-closure","fix":"Add host and daemon behavior plus focused acceptance for every deliberate pre-exec failure path, its exact status mapping, and absence of prepared/committed residue.","location":"Sections 2.2, 3.2, and 7.2 exec barrier acceptance","prevention":"Build an outcome table from pre-exec entry through host response and daemon settlement, including residue checks for each terminal branch.","principle":"A pre-exec gate is complete only when every child/host failure settles both protocol state and daemon-visible state without residue.","root_cause":"The replacement dropped source-plan cases for pre-exec death, errno variants, injected failures, bare EOF, and final daemon settlement.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"control-event-state-machine","description":"The plan omits listed-prepared, unreachable, superseded-attempt, large-line, and negative tmux-control-event cases; prescribes an invalid per-call `readline(limit=...)`; allows head-of-line blocking; and claims convergence with an unbounded gap-recovery buffer.","finding_id":"PR1-control-recovery-closure","fix":"Set the reader limit when opening the connection, use bounded concurrent dispatch with one bounded writer while preserving ledger order, restore every omitted recovery branch, bound recovery by entries and bytes, and define overflow as a repeated cut or equivalent deterministic re-reconciliation.","location":"Control/event recovery implementation and acceptance","prevention":"Review the state machine across API validity, concurrent requests, queue limits, overflow delivery, reconnect cuts, and every enumerated recovery branch.","principle":"A reconnecting protocol must define executable APIs, bounded buffering, non-blocking correlation, and a terminal transition for every recovery state.","root_cause":"Several source recovery branches were dropped, `StreamReader.readline(limit=...)` is not a valid Python API, an inline 30-second commit can block unrelated traffic, and recovery buffering has no overflow transition.","section_id":"3.2","severity":"blocking"},{"category":"missing-requirement","check_key":"packaging-requirement-parity","description":"The plan drops still-owned gterminal packaging and CI requirements, so a passing implementation could ship the wrong files, miss notices, require Zig by default, regress Windows lint, or duplicate managed-binary inventory.","finding_id":"PR1-packaging-parity","fix":"Add exact targets and focused acceptance for package-list parity, NOTICE/license contents, the default no-Zig build, Windows cross-lint, and a unique managed-binary inventory, or cite concrete landed/superseding evidence for each item.","location":"Sections 4.1–4.3 targets and acceptance","prevention":"Trace each source packaging invariant to an exact target and acceptance artifact or a verified landed disposition.","principle":"A packaging/CI replacement must preserve each still-owned distribution, licensing, toolchain, and inventory invariant.","root_cause":"P4 omits the source plan's package-list parity, NOTICE/license, default no-Zig, Windows cross-lint, and managed-binary inventory requirements without landed evidence.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"installer-input-matrix","description":"The shared installer abstraction is under-specified: both installer APIs accept `force`, and their source freshness, distribution, pin, and source-build paths are not represented or preserved.","finding_id":"PR1-installer-state-closure","fix":"Reconcile the complete existing input and consumer matrix, define `force` behavior for every published/present/version state, and add paired gterm/gclient acceptance for each state.","location":"Installer state table and acceptance","prevention":"Inventory both installer signatures and all source/build consumers, then test every relevant state-table row and override dimension.","principle":"A shared installer decision table must cover every existing input dimension and both consumers before replacing their logic.","root_cause":"The proposed table omits the existing `force` dimension and does not disposition source freshness, distribution, pin, and source-build behavior for both installers.","section_id":"4.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"orphan-write-durable-carrier","description":"The planned orphan sweep cannot find the very records it must settle after a daemon restart, and the acceptance does not close all authority-ordering, machine-scope, and singleton cases.","finding_id":"PR1-orphan-sweep-carrier","fix":"Put the machine-scoped prefix sweep on `TerminalManager` or the existing unresolved-write settlement storage, invoke it during wiring startup, and complete lock-order, singleton, and machine-scope acceptance.","location":"Startup orphan sweep and registry ownership","prevention":"For each restart-recovery requirement, verify that its owner can enumerate the persisted records before any new in-memory object exists.","principle":"Recovery after process death must discover durable state through a machine-scoped persistent authority, not an empty in-memory registry.","root_cause":"The startup sweep is assigned to `TerminalLeaseRegistry`, which cannot discover persisted `ws:` unresolved-write latches from a prior daemon epoch.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"indeterminate-input-proof","description":"The proposed capture-based reconciliation can both misclassify delivery and encourage unsafe duplicate input.","finding_id":"PR1-send-keys-indeterminate","fix":"Return the stored indeterminate result for same-key unresolved retries without redispatch unless the runtime supplies a real operation acknowledgement; do not add a cache or table merely to infer delivery from capture.","location":"Idempotent `send_keys` retry behavior","prevention":"For every indeterminate retry path, identify the runtime acknowledgement that proves delivery; absent one, preserve indeterminate without redispatch.","principle":"A retry may claim delivery only from an injective acknowledgement of the attempted operation; screen state is not such an acknowledgement for arbitrary input.","root_cause":"Capture cannot prove whether raw text, named keys, or control input was delivered, so it cannot safely turn an indeterminate write into a resolved outcome.","section_id":"5.2","severity":"blocking"},{"category":"missing-requirement","check_key":"web-input-branch-closure","description":"The web plan leaves later keystrokes, failure cleanup, roster/fragment limits, readiness, history, scrolling, accessibility, and window replacement behavior unverified.","finding_id":"PR1-web-input-closure","fix":"Restore or explicitly preserve every omitted source branch; either refuse additional pending input with visible feedback or define a bounded ordered accumulator; and specify cleanup for control failure, lease loss, blur, reconnect, and replacement.","location":"Sections 6.1–6.2 web input and roster acceptance","prevention":"Trace every source UI branch to a state transition and focused browser or unit acceptance, including disposal and replacement.","principle":"A UI concurrency fix must define every enqueue, replacement, failure, cleanup, readiness, and accessibility transition it inherits.","root_cause":"The QA 4.6 replacement drops control-failure, roster, fragment, readiness, history, scrolling, accessibility, and replacement-window branches, while a single `pendingInput` slot leaves later input undefined.","section_id":"6.1","severity":"blocking"},{"category":"over-engineering","check_key":"capacity-fixture-proportionality","description":"No requirement or consumer needs 188 generated test cases. That extra fixture machinery adds cost while failing to preserve the actual seventeen-test host contract.","finding_id":"PR1-capacity-test-misread","fix":"Keep or rewrite the seventeen named Rust live-host tests, use 124 native plus 64 tmux only as the population of the single list-envelope capacity scenario, and add a Python acceptance package only for independently stated Python behavior.","location":"Host acceptance package","prevention":"Classify source numerals as counts, bounds, fixtures, or test cases before expanding them into deliverables.","principle":"Test mechanism must correspond to a concrete requirement; population values for one capacity scenario are not a requested number of distinct tests.","root_cause":"The plan misreads 124 native plus 64 tmux terminals from one list-envelope capacity test as 188 test cases and drops the seventeen named Rust live-host tests.","section_id":"7.2","severity":"blocking"},{"category":"weak-testability","check_key":"flip-red-invalidation","description":"The checker could incorrectly approve after a later red row and still satisfy the current acceptance list.","finding_id":"PR1-flip-red-case","fix":"Add a case in which a later red row follows a qualifying same-commit macOS/Linux green pair and assert rejection under the documented row ordering.","location":"Native default-flip checker acceptance","prevention":"Mirror every positive gate predicate with each documented invalidating transition.","principle":"Every state-invalidating rule must have a negative acceptance case.","repairs":[{"items":[{"artifact":"test: `tests/config/test_native_backend_flip.py::test_flip_gate_rules`","prose":"A later red row after a qualifying same-commit macOS/Linux green pair causes the checker to fail under the documented row ordering."}],"kind":"add_acceptance","section_id":"8.1"}],"root_cause":"The prose says a later red row invalidates a qualifying green pair, but acceptance tests only green and incomplete-green cases.","section_id":"8.1","severity":"blocking"},{"category":"traceability","check_key":"leaf-close-gate-propagation","description":"All 18 leaf tasks can close without Guard set G even though Constraint 10 declares it mandatory.","finding_id":"PR1-guard-g-propagation","fix":"Repeat the applicable Guard set G command or criterion in every deliverable section so manifest derivation places it in every leaf's validation criteria.","location":"Global Guard set G versus all 18 deliverable acceptance lists","prevention":"Inspect the derived manifest and verify every mandatory global close gate appears in each affected leaf's validation criteria.","principle":"A close gate that every leaf must run must be present in each leaf's derived validation criteria.","root_cause":"Manifest derivation carries only per-deliverable acceptance, so the global constraint never reaches any leaf agent.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"c62b1ffa-fee6-4802-bd2b-3de410dd6fb5","round":1,"verdict":"needs_review"},"session_id":"ef4ec05c-1fa4-41af-908a-472af0750d3c"}
```

**Round 1 human handoff** `kind: verification`

- evidence: e9a4f711-e6e2-4f39-8ed5-f12467987346 (finalized `needs_review`, round cap 1 reached)
- repairs applied: typed repair for PR1-flip-red-case (8.1.3) through `apply_plan_review_repairs`; the twelve prose-only findings hand-applied to Overview, Constraint 5, 1.1, 1.2, 1.3, 2.1, 2.2, 3.1, 3.2, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 6.1, 6.2, 7.1, 7.2, and 8.1, including one Guard set G close-gate acceptance item per deliverable
- base validation after repairs: `uv run gobby plans validate .gobby/plans/native-runtime-completion.md -p d45545c5-ded5-4335-b115-0245752edacf` exit 0 (advisory consumer-coverage warnings explained by Constraint 11)
- next: no further adversary round; the repaired artifact waits for the human decision (continue interactively, approve for implementation through the coordinator handoff manifest route, or stop)

**Round 2** `kind: verification`

- reviewer_run: 9e8302e0-9e9f-46e8-8003-a1c67531c35f (codex gpt-6-astra, reasoning xhigh, max_review_rounds 3)
- reviewer_session: a642b2e1-ac9e-4095-82b9-ba0e77c07dae
- verdict: needs_review
- findings:
- PR2-spawn-alias-consumer / blocking / 1.1 removes `tmux_session_name` while `_promote_prepared` still passes it and is owned by 1.2 — accepted (typed repair: `_promote_prepared` joins 1.1)
- PR2-native-error-decoding / blocking / 1.2 outcome table lacks `exec_timeout` and `malformed_status` rows and `raise_for_payload` discards code, detail, and stage — accepted; two rows, `HostCommandError` and `raise_for_payload` Targets, decoded-reply acceptance
- PR2-gate-early-exit / blocking / 2.2 EOF plus `waitpid(WNOHANG)` labels a fast successful child `exec_failed:gate_died` (fixer-induced by the round 1 repair for PR1-gate-preexec-closure) — accepted (modified); the status pipe guarantees only that no pre-exec error was reported, an already-exited child is `committed` and settled through the exit path with its wait status, `gate_died` is dropped, and no separate indeterminate state is added because signal death before and after exec is observably identical
- PR2-execvp-shell-fallback / blocking / 2.2 `execvp` runs `/bin/sh` on `ENOEXEC` so 2.2.1 cannot observe `exec_failed:ENOEXEC` — accepted; PATH is resolved by the gate and the final handoff is `execve`
- PR2-cleanup-resource-epoch / blocking / 1.3 delayed native kills by reused `ht-N` can hit a respawned host's replacement — accepted; `terminate_host_id` takes the row's `host_epoch` and refuses a mismatch, no new locks
- PR2-hostproc-callers / blocking / 3.1 `HostProc` return type change leaves `frame_protocol::start_host`, `control_protocol::authed`, and `wait_exit` untargeted — accepted (typed repair)
- PR2-lint-scope-and-splits / blocking / 4.2 guard fails on an existing allow inventory outside its Targets and two 900-line platform files — accepted; Targets extended to every file a `--force-warn` clippy run surfaces (163 warnings in 24 files) plus split targets `platform/macos_process.rs` and `platform/windows_daemon.rs`, guard policy unchanged
- PR2-coordinator-lock-callers / blocking / 5.1 deletes `_lock` while `write`, `run_sequence`, and `run_native_wake_batch` still call it — accepted (typed repair)
- PR2-operator-write-authority / blocking / 5.1 `_deliver_operator_write` dispatches straight to the runtime and bypasses the coordinator lock and latch — accepted; every operator write routes through the injected coordinator
- PR2-orphan-epoch-metadata / blocking / 5.1 latch entries carry no daemon epoch so the orphan sweep cannot select by epoch (fixer-induced by the round 1 repair for PR1-orphan-sweep-carrier) — accepted; the `ws:` latch entry gains the writer daemon epoch
- dismissed by the reviewer: one fingerprint-timing candidate in 5.2, because the existing write-ahead latch ordering already suffices
- resolution_notes: All ten findings accepted, one modified (PR2-gate-early-exit drops the `gate_died` inference without adding an indeterminate state). Every vote walked the restraint ladder and the user confirmed the votes as proposed. Typed repairs applied through `apply_plan_review_repairs`; the seven prose-only findings hand-applied to 1.2, 1.3, 2.2, 3.2, 4.2, and 5.1. Round cap is 3; round 3 remains available.

```json plan-review-round
{"evidence_id":"c3f531de-b33d-48d7-bb5a-029c452a03aa","plan_hash":"811c46333bd624f789a925e1debe3de1d5f649b6142e6fb7adccc0e9bba152e5","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"77d805e531a52faf4255865289eb0054163d878fa3fb2a156b6359b50444751f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":10,"total":11},"evidence_id":"c3f531de-b33d-48d7-bb5a-029c452a03aa","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"871c983efdec05a214f1f9d10b27d2d28891ac71658a169e20d599d81d04a87d","status":"valid"},"source_digest":"5aca9769cd5d917e48f4a4e1c5492f08050843ff2ca627d6e80cad85c9b9e330","version":1},"findings":[{"category":"traceability","check_key":"removed-field-consumers-close-together","description":"1.1 removes tmux_session_name, but _promote_prepared still passes it for both native and tmux success results. That method first appears in dependent 1.2, so 1.1 would make successful spawns raise TypeError before its close gate can pass.","finding_id":"PR2-spawn-alias-consumer","fix":"Add _promote_prepared to 1.1 and remove its alias keyword alongside SpawnResult. Keep 1.2's later typed-outcome edits ordered after 1.1.","location":"1.1 Targets; spawn_executor.py::_promote_prepared success return","participating_section_ids":["1.1","1.2"],"prevention":"Sweep every constructor keyword and verify its caller is owned by the removal leaf, not merely a later dependent leaf.","principle":"A removed constructor field and every production constructor using it must migrate in the same closeable leaf.","repairs":[{"entries":["`src/gobby/agents/spawn_executor.py::_promote_prepared`"],"kind":"add_targets","section_id":"1.1"}],"section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"spawn-outcome-branch-closure","description":"2.2 introduces exec_timeout and malformed_status after killing the child and removing its slot, but 1.2's single-source outcome table maps neither. The landed raise_for_payload also discards errno code, detail, and stage, and its required change is not targeted. Host-side tests could pass while daemon rows or API failures remain untyped.","finding_id":"PR2-native-error-decoding","fix":"Add exec_timeout and malformed_status rows with bounded codes and fail_pending settlement. Preserve code/detail/stage in the existing HostCommandError and HostClient.raise_for_payload path, adding those exact Targets. Add decoded host-reply tests covering both outcomes and errno detail through agent and web settlement.","location":"1.2 outcome table and HostClient decoding; 2.2 timeout/malformed-status responses","participating_section_ids":["1.2","2.2","3.2"],"prevention":"Trace each host negative response through the real decoder, classifier, agent result, web result, and row settlement.","principle":"Every definitive host failure must retain its structured cause and settle the daemon row through an explicit typed outcome.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-gate-preexec-closure","causal_section_ids":["2.2"],"check_key":"preexec-status-closure","description":"A child can exec /usr/bin/true, close fd 4 through CLOEXEC, and exit 0 before the host runs. EOF followed by waitpid(WNOHANG) then returns the child and status 0, which this plan calls exec_failed:gate_died and deletes. A bounded local diagnostic reproduced this exact sequence. The waitpid heuristic does not establish the claimed proof of exec.","finding_id":"PR2-gate-early-exit","fix":"Keep the status pipe and state its observable guarantee: no pre-exec error was reported. Remove the inference that an already-exited child means gate_died; preserve its wait status and use the exit-settlement path. Define indistinguishable pre-/post-exec signal death as indeterminate, and align the 1.2 outcome mapping and 3.2 settlement with that decision. Add fast successful, fast nonzero, and pre-exec-death acceptance without adding tracing machinery.","introduced_in_round":1,"location":"2.2 EOF/waitpid(WNOHANG) rule and acceptance 2.2.3–2.2.5","prevention":"Exercise a successful image that exits before the host reads its exec-status pipe, including zero and nonzero exit statuses.","principle":"A process that has already exited does not prove that exec failed; launch observation and process exit are distinct facts.","root_cause":"The round-1 repair treats every child reaped after status-pipe EOF as a pre-exec death.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"exec-image-error-contract","description":"execvp handles an unrecognized executable header by executing /bin/sh, as documented by the local exec(3) manual. Executable text that produces ENOEXEC from execve can therefore launch a shell instead of returning the required exec_failed:ENOEXEC. The proposed primitive contradicts 2.2.1.","finding_id":"PR2-execvp-shell-fallback","fix":"Resolve PATH candidates and use execve for the final handoff without execvp's shell fallback. Keep the existing status pipe and errno mapping, update the stage name consistently, and test executable text without a recognized header alongside ENOENT and EACCES.","location":"2.2 gterm gate execvp choice and acceptance 2.2.1","participating_section_ids":["2.2"],"prevention":"Check the platform execution API's fallback behavior for executable text, missing files, and permission failures.","principle":"The execution primitive must preserve the error semantics promised by the API.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"destructive-cleanup-resource-epoch","description":"HostState resets next_host_id to 1 for each new host. An old pending attempt can retain ht-1 while respawn creates a different terminal named ht-1. The delayed reaper or web timeout then kills that replacement through the manager's current client; attempt-generation CAS happens after the destructive side effect and cannot prevent it.","finding_id":"PR2-cleanup-resource-epoch","fix":"Capture and persist the originating host epoch with host_terminal_id. Bind every delayed native kill to that epoch at dispatch and refuse mismatches; reconnect must retain the captured epoch instead of substituting the manager's current one. Apply this to reaper, timeout, and related cleanup paths and add a deterministic restart/reused-ht-1 test. Reuse existing epoch checks; no additional settlement locks are needed.","location":"1.3 terminate_host_id and delayed web cleanup; 1.2 respawn; 3.2 reconnect","participating_section_ids":["1.2","1.3","3.2"],"prevention":"Restart the owner while a cleanup is paused, reuse the local resource ID, and verify the old cleanup cannot touch the replacement.","principle":"A destructive operation must use the complete captured resource identity, including the epoch in which an identifier was allocated.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"shared-return-type-consumers-close-together","description":"3.1 changes spawn_host to return HostProc, while frame_protocol::start_host still returns (TempDir, Child) and control_protocol::authed returns (Child, UnixStream). frame_protocol is absent from all Targets and control_protocol is not owned by 3.1. The shared wait_exit helper also takes &mut Child. These migrations cannot wait until 3.2.","finding_id":"PR2-hostproc-callers","fix":"Own frame_protocol and control_protocol's typed helpers and child operations in 3.1, and migrate wait_exit with the shared owner. Preserve the existing dependency order with other control_protocol owners.","location":"3.1 HostProc migration Targets","participating_section_ids":["3.1","3.2"],"prevention":"Inspect all direct helper callers and explicit return types before changing the shared test owner.","principle":"A shared return-type change and its explicitly typed callers must compile in the same leaf.","repairs":[{"entries":["`crates/gterminal/tests/frame_protocol.rs::*` — scope-reason: migrate the typed host owner and all affected child operations to HostProc","`crates/gterminal/tests/control_protocol.rs::*` — scope-reason: migrate authed and all affected child operations with the shared HostProc return type","`crates/gterminal/tests/host_support/mod.rs::wait_exit`"],"kind":"add_targets","section_id":"3.1"}],"section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"new-source-guard-inventory","description":"The planned guard rejects existing allowances outside its Targets: dead_code occurs in host/frames, input/encode/model/parse, kitty_graphics/host_paint/host_stream, pane/osc, pty/actor/backend/mod, and raw_input. Required reason annotations also reach input/mod, pane/runtime, platform/macos/windows, and protocol/wire_types. macos.rs and windows.rs contain 916 and 935 production lines, so adding their Targets also requires decomposition. Removing lib.rs's blanket allow cannot make 4.2 pass.","finding_id":"PR2-lint-scope-and-splits","fix":"Extend 4.2's exact or justified whole-file Targets to this existing allow inventory and own each removal, code fix, or reason annotation. Add same-leaf split targets and explicit move prose for macos.rs and windows.rs under the repository size rule, accounting for state.rs allowances through prerequisite 2.1's extraction. Keep the declared guard policy. This needs a complete scope revision; add_targets alone is insufficient.","location":"4.2 Targets and no_blanket_lint_allows policy","participating_section_ids":["2.1","4.2"],"prevention":"Run a bounded literal sweep for the proposed guard predicate, inventory every matching handwritten file, and apply the size gate to the resulting scope.","principle":"A new source-wide guard must own every existing violation it necessarily turns into a failure.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"removed-lock-consumers-close-together","description":"WriteCoordinator.write, run_sequence, and run_native_wake_batch still call self._lock, which 5.1 deletes. None is targeted by 5.1; write is assigned to later 5.2 and the other two are absent. Ordinary writes and wake operations would raise AttributeError at the 5.1 boundary.","finding_id":"PR2-coordinator-lock-callers","fix":"Add all three methods to 5.1 and acquire the registry's per-terminal lock directly. Preserve sorted acquisition and AsyncExitStack release for native batches, using the existing sequence and batch tests.","location":"5.1 _lock deletion and Targets","participating_section_ids":["5.1","5.2"],"prevention":"Sweep every _lock call, including sequences and multi-terminal batches, and assign each migration to the removal leaf.","principle":"Replacing lock ownership must migrate every surviving lock acquisition before the old helper is removed.","repairs":[{"entries":["`src/gobby/terminals/write_coordinator.py::WriteCoordinator.write`","`src/gobby/terminals/write_coordinator.py::WriteCoordinator.run_sequence`","`src/gobby/terminals/write_coordinator.py::WriteCoordinator.run_native_wake_batch`"],"kind":"add_targets","section_id":"5.1"}],"section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"operator-writes-share-authority-lock","description":"The real operator path calls runtime.write_input/write_paste/write_text directly whenever runtime resolution succeeds. coordinator.write is only the fallback. Injecting one registry leaves that branch outside its lock and durable latch, so takeover or finalize can change authority while a previously admitted web write is in flight, contradicting 5.1.9.","finding_id":"PR2-operator-write-authority","fix":"Target _deliver_operator_write in 5.1 and route every operator write through the injected coordinator. Preserve typed runtime-unavailable handling and remove the direct dispatch bypass. Exercise input, paste, and text with a resolved runtime paused during the existing authority-mutation race tests; use the existing registry lock.","location":"5.1 operator routing and 5.1.9; TerminalWsMixin._deliver_operator_write","participating_section_ids":["5.1","6.1"],"prevention":"Test the resolved production runtime branch with dispatch paused while takeover, release, finalize, and exit cleanup race.","principle":"Every writer must pass through the authority and persistence boundary whose ordering the plan promises.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"PR1-orphan-sweep-carrier","causal_section_ids":["5.1"],"check_key":"orphan-sweep-durable-selection-data","description":"Existing unresolved-write entries contain only at and origin, and ws keys contain an opaque attachment ID and sequence. A fresh registry cannot reconstruct the old attachment's daemon epoch. The proposed sweep therefore cannot distinguish old from current epochs as 5.1.4 requires.","finding_id":"PR2-orphan-epoch-metadata","fix":"Persist the writer daemon epoch on ws entries in the existing write-ahead latch, using the terminal row's machine ownership for machine scope. Pass it from the real operator path through the coordinator and fake store, include the affected persistence Targets in 5.1, and test old/current epochs after an actual store round trip. Reuse the one latch carrier and startup sweep; no attachment-history table is needed.","introduced_in_round":1,"location":"5.1 clear_orphaned_attachment_writes and acceptance 5.1.4","prevention":"Construct cleanup inputs through the real persistence API, restart with an empty registry, and verify every selection field remains available.","principle":"A restart-time selection predicate must be supported by data persisted before the previous process disappeared.","root_cause":"The round-1 fix moved orphan cleanup into storage without supplying the daemon epoch needed by its selection predicate.","section_id":"5.1","severity":"blocking"}],"reviewer_session":"a642b2e1-ac9e-4095-82b9-ba0e77c07dae","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"ef4ec05c-1fa4-41af-908a-472af0750d3c"}
```

**Round 3** `kind: verification`

- reviewer_run: 46d18a17-abd6-45a9-b504-136d70696ded (codex gpt-6-astra, reasoning xhigh, max_review_rounds 3; a first round-3 attempt, run 383f7ac9-fea7-434b-9b64-81d6309c8311, stalled without progress events, was stopped by the daemon, and its evidence expired unfinalized, so it never counted)
- reviewer_session: d061d272-42f5-42d2-86fd-860101a57082
- verdict: needs_review
- findings:
- PR3-response-builder-test-consumer / blocking / 1.1 removes `_build_spawn_success_response` while `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py` still imports and calls it in three tests — accepted (typed repair: the test file joins 1.1 and migrates to `build_spawn_response`)
- PR3-latch-epoch-contract-consumers / blocking / 5.1 makes `daemon_epoch` required on `persist_unresolved_write` but the `UnresolvedWriteStore` Protocol and two direct `test_wake_native_terminal.py` callers are untargeted (fixer-induced by the round 2 repair for PR2-orphan-epoch-metadata) — accepted (typed repairs: the Protocol method joins 5.1 and 5.2, the two wake tests join 5.1)
- PR3-child-exit-status-owner / blocking / 2.2 promises a stored wait status for a child already reaped at commit, but `PaneRuntime`'s existing waiter discards the status in both construction paths and 3.2's watcher would have to wait a second time — accepted (prose-only); `PaneRuntime`'s waiter becomes the sole reap owner, retains the exit result, and exposes it through a `child_exit` accessor that 3.2's watcher observes instead of calling wait or waitpid again; no new launch state or reaper
- dismissed by the reviewer: two candidates (one blast-radius, one runtime-invariant) judged already covered
- resolution_notes: All three findings accepted; every vote walked the restraint ladder (findings 1 and 2 at rung 6, target lines only; finding 3 at rung 2, reusing the existing waiter). The user approved the votes and directed the plan to expansion. Typed repairs applied through `apply_plan_review_repairs`; finding 3 hand-applied to 2.2 and 3.2. Round cap of 3 reached: no further adversary round; the manifest comes from the explicit human-handoff route.

```json plan-review-round
{"evidence_id":"611fce17-297e-4a28-a969-0d3a952df3b3","plan_hash":"51e02a0444fc347ea985beae54f7d9fca4645a1b727c215ffefe8c97df1697b5","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"6501942c7c2e9dc533a5ee259eae7882d3d0d55d27438c3c95210dae1ab7c561","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":3,"total":5},"evidence_id":"611fce17-297e-4a28-a969-0d3a952df3b3","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":18,"manifest_digest":"371706f3b274a2ea7d0495cfbeb04ad2f0ba5f09aac8f4789cac0f0e325119ac","status":"valid"},"source_digest":"21e13c9fba102e4a638d2d62cd0a6188878a5bc6abdfc194f58400906daa65ef","version":1},"findings":[{"category":"traceability","check_key":"response-builder-contract","description":"The deterministic report's omitted response-builder consumer is not unchanged: tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py imports _build_spawn_success_response from _runtime at line 10 and calls it in three tests. Section 1.1 removes that symbol and module location but never targets this test file, so the promised removal leaves a collection-time ImportError.","finding_id":"PR3-response-builder-test-consumer","fix":"Add the existing response-builder test file to 1.1 and migrate its import and three calls to _response.build_spawn_response while preserving the response assertions.","location":"1.1 Targets and removal of _build_spawn_success_response","participating_section_ids":["1.1"],"prevention":"Run a literal sweep for the removed name and disposition every production and test import before accepting unchanged-consumer exclusions.","principle":"Removing or renaming a symbol must migrate every surviving import in the same leaf.","repairs":[{"entries":["`tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py::*` — scope-reason: migrate the removed response-builder import and all three response tests to _response.build_spawn_response"],"kind":"add_targets","section_id":"1.1"}],"section_id":"1.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"PR2-orphan-epoch-metadata","causal_section_ids":["5.1"],"check_key":"required-latch-metadata-consumers-close-together","description":"WriteCoordinator types its store as UnresolvedWriteStore, whose persist_unresolved_write declaration still accepts only at as a keyword. It is absent from 5.1/5.2 Targets, so passing 5.1's required daemon_epoch (and 5.2's fingerprint) fails the source type gate. tests/events/test_wake_native_terminal.py also calls the fake method without an epoch at lines 387 and 416; those calls must fail under 5.1.12 and the file is not targeted. Constraint 11's unchanged-call-site rationale cannot cover these required-argument consumers.","finding_id":"PR3-latch-epoch-contract-consumers","fix":"Target UnresolvedWriteStore.persist_unresolved_write in 5.1 for the required daemon_epoch and in 5.2 for the optional fingerprint. Target the two direct wake-test callers in 5.1 and pass explicit fixture epochs, retaining their existing wake-settlement assertions. Use the already planned store, fake, and coordinator changes; no new carrier is needed.","introduced_in_round":2,"location":"5.1 required daemon_epoch argument; 5.2 fingerprint extension","prevention":"Sweep the changed persistence method across protocol declarations, concrete stores, subclasses, and direct test calls, then assign each migration to the signature-changing leaf.","principle":"A required persistence parameter must migrate its protocol and direct callers alongside its implementations.","repairs":[{"entries":["`src/gobby/terminals/write_coordinator.py::UnresolvedWriteStore.persist_unresolved_write`","`tests/events/test_wake_native_terminal.py::test_latched_wake_is_settled_by_the_delivered_composer_clear`","`tests/events/test_wake_native_terminal.py::test_undelivered_composer_clear_leaves_the_earlier_wake_latched`"],"kind":"add_targets","section_id":"5.1"},{"entries":["`src/gobby/terminals/write_coordinator.py::UnresolvedWriteStore.persist_unresolved_write`"],"kind":"add_targets","section_id":"5.2"}],"root_cause":"The round-2 epoch repair updated the concrete store and fake Targets but omitted their typed protocol and two direct wake-test calls.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"single-owner-child-exit-status","description":"PaneRuntime already starts the child waiter before commit: spawn_command_builder consumes child.wait() and keeps only an AtomicBool, while from_master_fd consumes waitpid and also discards its status. Neither path exposes the exit result. The new watcher specified in native_ops at commit therefore cannot reliably obtain 2.2's already-reaped status: a second wait races the existing owner or returns ECHILD. The fast-exit and exact exit_code requirements cannot be implemented within the declared host-only Targets.","finding_id":"PR3-child-exit-status-owner","fix":"Make PaneRuntime's existing waiter the sole reap owner and retain/expose its completion result, including a result produced before commit. Add the relevant PaneRuntime struct/construction/accessor Targets to 2.2, and have 3.2's host watcher observe that retained completion rather than call wait/waitpid again. Keep the current committed-then-exited policy and existing zero/nonzero/signal acceptance cases; no third launch state or extra reaper is needed.","location":"2.2 stored wait-status contract and 3.2 native_ops child-wait watcher","participating_section_ids":["2.2","3.2"],"prevention":"Trace both PTY construction paths through their existing waiters and verify a late subscriber can obtain zero, nonzero, and signal outcomes without waiting on the child again.","principle":"A child has one reap owner; exit-status consumers must observe that owner's retained result.","section_id":"2.2","severity":"blocking"}],"reviewer_session":"d061d272-42f5-42d2-86fd-860101a57082","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"ef4ec05c-1fa4-41af-908a-472af0750d3c"}
```

**Human handoff** `kind: verification`

- trigger: review cap of 3 rounds reached with round 3 finalized `needs_review`; no further adversary round
- accepted repairs applied after the round 3 checkpoint: typed repairs for PR3-response-builder-test-consumer (1.1) and PR3-latch-epoch-contract-consumers (5.1, 5.2); PR3-child-exit-status-owner hand-applied to 2.2 (four `PaneRuntime` Targets, sole-reap-owner prose) and 3.2 (watcher observes the retained result)
- approval: the user approved the round 3 votes and directed expansion in plain text ("apply the changes, then prep for expansion using the gobby-skills expand skill, then expand the epic")
- manifest route: explicit human handoff through `derive_plan_handoff_manifest` and `apply_plan_handoff_manifest`; no adversary verdict, findings, or coverage attestation are manufactured for it
- validation: `uv run gobby plans validate .gobby/plans/native-runtime-completion.md -p /Users/josh/Projects/gobby` (base) before the manifest apply, then `--mode expansion` after it

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Remove `SpawnResult` tmux aliases and route liveness and cleanup through
    the runtime
  category: refactor
  task_type: refactor
  depends_on: []
  validation_criteria: '1.1.1: Neither `SpawnResult` has a `tmux_session_name` or
    `tmux_pane` attribute and constructing either with those keywords raises `TypeError`.
    symbol: `SpawnResult`. test: `tests/agents/test_spawn_executor.py::test_spawn_result_has_no_tmux_aliases`.

    1.1.2: Spawn-agent health checks a native row through `NativeTerminalRuntime.is_live`
    and a tmux row through `TmuxTerminalRuntime.is_live`, never by pane. test: `tests/mcp_proxy/tools/spawn_agent/test_response.py::test_health_uses_runtime_liveness_per_backend`.

    1.1.3: Failure cleanup terminates through the row''s runtime and settles the row
    (`fail_pending` for pending, `mark_exited` for live); no `tmux kill-session` is
    invoked from `_failure_cleanup.py`, and SIGKILL still requires a matching pid
    start time. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_terminates_via_runtime_and_settles_row`.

    1.1.4: `_FIELD_SWEEP_ALLOWED` contains no `src/gobby/` path and the sweep passes.
    test: `tests/terminals/test_no_direct_tmux_consumers.py::test_tmux_session_name_field_sweep`.

    1.1.5: Resume writes `SPAWN_KEY_KEY` from the terminal row and the MCP success
    response is built by `build_spawn_response` in `_response.py`; no `_build_spawn_success_response`
    symbol remains. symbol: `build_spawn_response`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_response.py`.

    1.1.6: Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:1.1:1.1.1
  - covers:native-runtime-completion:1.1:1.1.2
  - covers:native-runtime-completion:1.1:1.1.3
  - covers:native-runtime-completion:1.1:1.1.4
  - covers:native-runtime-completion:1.1:1.1.5
  - covers:native-runtime-completion:1.1:1.1.6
  tdd: false
  source_section: '1.1'
  assigned_agent: backend-developer
- title: Typed native spawn failure outcomes and singleflight host respawn
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: "1.2.1: Each row of the outcome table yields the named `SpawnResult.error`\
    \ code and row settlement; `commit_indeterminate` leaves the row pending. test:\
    \ `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table`.\n\
    1.2.2: `HostClient._roundtrip` raises `CommitTransportError(request_written=False)`\
    \ when the write fails and `request_written=True` when the read fails after a\
    \ complete write; other verbs raise `HostUnavailableError`. test: `tests/terminals/test_host_client.py::test_commit_transport_error_reports_written_state`.\n\
    1.2.3: After an unexpected host death `ensure_restart` respawns once for concurrent\
    \ callers, backs off 1, 2, 4 \u2026 30 s, resets after 60 s healthy, and after\
    \ five failures raises `HostManagerStopped` and sets `native_available=False`;\
    \ a drained or stopping manager never respawns. test: `tests/terminals/test_host_manager.py::test_ensure_restart_is_singleflight_with_backoff`,\
    \ `tests/terminals/test_host_manager.py::test_drained_host_is_never_respawned`.\n\
    1.2.4: `terminal_create_result` carries `code` of at most 128 characters and `reason`\
    \ detail for every failure branch, served from `TerminalCreateMixin`. test: `tests/servers/test_terminal_ws_create.py::test_create_result_code_is_bounded`.\n\
    1.2.5: `TerminalHostConfig` validates `restart_max_attempts >= 1` and `restart_backoff_ceiling_seconds\
    \ > 0`, and the checked-in runtime config contract carries both fields. test:\
    \ `tests/config/test_terminals.py::test_host_restart_fields_validate`, `tests/config/test_runtime_config_contract.py::test_checked_in_contract_matches_registry`.\n\
    1.2.6: Two waiters share one restart during backoff; cancelling one raises `CancelledError`\
    \ in that waiter only, the survivor receives the new epoch, and exactly one host\
    \ spawn occurs. test: `tests/terminals/test_host_manager.py::test_waiter_cancellation_does_not_cancel_shared_restart`.\n\
    1.2.7: `stop` during backoff prevents late client publication, every caller arriving\
    \ during or after teardown receives `HostManagerStopped` without a spawn or token\
    \ rotation, and a later explicit `start` permits one fresh restart; the test drives\
    \ a fake monotonic clock and controlled sleep futures with no wall-clock wait.\
    \ test: `tests/terminals/test_host_manager.py::test_stop_fences_restart_creation_and_publication`.\n\
    1.2.8: Outcome row `reserve refused`: for each of `host_draining`, `capacity`,\
    \ `stale`, and `not_native`, `SpawnResult.error` is `host_refused:<reason>` with\
    \ the host's exact reason and the row is failed through `fail_pending`. test:\
    \ `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[reserve_refused]`.\n\
    1.2.15: Outcome row `host epoch changed`: `host_epoch_changed`, `fail_pending`,\
    \ and the row is never promoted even when the new epoch lists a terminal with\
    \ the old id. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[host_epoch_changed]`.\n\
    1.2.16: Outcome row `cancelled before write`: against a real `HostClient` over\
    \ a paused socket, a spawn task cancelled while blocked before `writer.write()`\
    \ raises `CancelledError` to its caller, returns no `SpawnResult`, drops its correlation\
    \ entry, kills the host row by id, and leaves the row failed `commit_not_sent`.\
    \ test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[cancelled_before_write]`.\n\
    1.2.17: Outcome row `cancelled after write`: a spawn task cancelled after `writer.write()`\
    \ of the commit request raises `CancelledError` to its caller, returns no `SpawnResult`,\
    \ and leaves the row pending `commit_indeterminate` for the 3.2 list cut. test:\
    \ `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[cancelled_after_write]`.\n\
    1.2.18: Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with\
    \ the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:\
    \ \"Guard set G\" in `docs/guides/gterminal-development-guide.md`.\n1.2.9: Outcome\
    \ row `prepare transport error before write`: `host_unreachable` and `fail_pending`.\
    \ test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[prepare_unreachable]`.\n\
    1.2.10: Outcome row `commit transport error, request not written`: `commit_not_sent`,\
    \ `fail_pending`, and the host row is killed by its id. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[commit_not_sent]`.\n\
    1.2.11: Outcome row `commit transport error, request written`: `commit_indeterminate`\
    \ and the row stays pending for list settlement. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[commit_indeterminate]`.\n\
    1.2.12: Outcome row `commit refused`: `commit_refused:<reason>` carries the host's\
    \ reason and the row is failed through `fail_pending`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[commit_refused]`.\n\
    1.2.13: Outcome row `exec failure from the gate`: `exec_failed:<code>` with the\
    \ errno text in `error_detail` and `fail_pending`. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[exec_failed]`.\n\
    1.2.14: Outcome row `host stopped or manager stopped`: `host_stopped` and `fail_pending`.\
    \ test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[host_stopped]`.\n\
    1.2.19: Outcome row `commit deadline expired inside the host`: a host reply of\
    \ `exec_timeout` yields `SpawnResult.error=\"exec_timeout\"` and `fail_pending`\
    \ with no daemon-side kill, because the host has already removed the slot. test:\
    \ `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[exec_timeout]`.\n\
    1.2.20: Outcome row `unparseable exec status from the gate`: a host reply of `malformed_status`\
    \ yields `SpawnResult.error=\"malformed_status\"` and `fail_pending` with no daemon-side\
    \ kill. test: `tests/agents/test_native_spawn.py::test_native_spawn_failure_outcome_table[malformed_status]`.\n\
    1.2.21: `raise_for_payload` on a reply carrying `code`, `detail`, and `stage`\
    \ raises `HostCommandError` exposing all three, and an `exec_failed` reply reaches\
    \ the agent `SpawnResult` as `error=\"exec_failed:ENOENT\"` with `error_detail`\
    \ naming the strerror text and stage, and reaches `terminal_create_result` as\
    \ `code=\"exec_failed:ENOENT\"` with the same `reason`. test: `tests/terminals/test_host_client.py::test_raise_for_payload_preserves_structured_error`,\
    \ `tests/servers/test_terminal_ws_create.py::test_create_result_carries_host_code_and_detail`."
  labels:
  - covers:native-runtime-completion:1.2:1.2.1
  - covers:native-runtime-completion:1.2:1.2.2
  - covers:native-runtime-completion:1.2:1.2.3
  - covers:native-runtime-completion:1.2:1.2.4
  - covers:native-runtime-completion:1.2:1.2.5
  - covers:native-runtime-completion:1.2:1.2.6
  - covers:native-runtime-completion:1.2:1.2.7
  - covers:native-runtime-completion:1.2:1.2.8
  - covers:native-runtime-completion:1.2:1.2.15
  - covers:native-runtime-completion:1.2:1.2.16
  - covers:native-runtime-completion:1.2:1.2.17
  - covers:native-runtime-completion:1.2:1.2.18
  - covers:native-runtime-completion:1.2:1.2.9
  - covers:native-runtime-completion:1.2:1.2.10
  - covers:native-runtime-completion:1.2:1.2.11
  - covers:native-runtime-completion:1.2:1.2.12
  - covers:native-runtime-completion:1.2:1.2.13
  - covers:native-runtime-completion:1.2:1.2.14
  - covers:native-runtime-completion:1.2:1.2.19
  - covers:native-runtime-completion:1.2:1.2.20
  - covers:native-runtime-completion:1.2:1.2.21
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Backend-aware reaper, attempt-owned timeouts, and settle CAS helpers
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: A stale native pending row is reaped through `terminate_host_id`
    with its recorded host id and host epoch and a stale tmux row through `kill_spawn_key`;
    a native row lacking `host_terminal_id` is failed with no kill call. test: `tests/agents/test_spawn_executor.py::test_reaper_terminates_by_backend_identity`.

    1.3.9: With a pending row holding `ht-1` from host epoch A, the host respawns
    as epoch B and allocates `ht-1` to a new live terminal; the paused reaper, the
    agent timeout callback, and the web timeout callback each refuse to kill (`HostEpochMismatch`,
    zero `kill` requests reach the host), epoch B''s `ht-1` stays live, and the old
    row is failed `host_epoch_changed`. test: `tests/agents/test_spawn_executor.py::test_delayed_kill_refuses_reused_host_id_after_respawn`,
    `tests/terminals/test_native_runtime.py::test_terminate_host_id_requires_matching_epoch`.

    1.3.2: A spawn timeout firing after `bump_attempt_generation` does not fail the
    newer attempt, on both the agent and the web spawn path. test: `tests/agents/test_spawn_executor.py::test_timeout_callback_is_owned_by_attempt_generation`.

    1.3.3: `settle_exit` moves pending to exited and live to exited only when `host_terminal_id`
    matches and is a no-op otherwise; concurrent `settle_promotion` and `settle_exit`
    on one row serialize under `settle_lock` and leave exactly one terminal state.
    test: `tests/storage/test_terminals.py::test_settle_exit_is_guarded_cas`, `tests/storage/test_terminals.py::test_settle_lock_serializes_promotion_and_exit`.

    1.3.4: `record_process` without `host_terminal_id` raises `ValueError`, with it
    merges only onto the current attempt, and `merge_process_reap_record` never touches
    other keys. test: `tests/storage/test_terminals.py::test_record_process_is_attempt_cas_merge`.

    1.3.5: `bump_attempt_generation` removes `host_terminal_id` from `process` and
    `retry_attempt_unsettled` refuses a settled row. test: `tests/storage/test_terminals.py::test_bump_attempt_drops_host_id_and_retry_refuses_settled`.

    1.3.6: A stale native pending row whose `terminate_host_id` raises `HostUnavailableError`
    stays pending with its process record intact and is reaped on the next sweep once
    the host answers. test: `tests/agents/test_spawn_executor.py::test_reaper_leaves_unreachable_native_row_pending`.

    1.3.7: A tmux retry whose `create_session` reports the session name pre-existing
    kills the `spawn_key` session before the attempt is failed, and no session named
    `spawn_key` survives. test: `tests/agents/test_spawn_executor.py::test_tmux_retry_kills_duplicate_session_before_failing`.

    1.3.8: Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:1.3:1.3.1
  - covers:native-runtime-completion:1.3:1.3.9
  - covers:native-runtime-completion:1.3:1.3.2
  - covers:native-runtime-completion:1.3:1.3.3
  - covers:native-runtime-completion:1.3:1.3.4
  - covers:native-runtime-completion:1.3:1.3.5
  - covers:native-runtime-completion:1.3:1.3.6
  - covers:native-runtime-completion:1.3:1.3.7
  - covers:native-runtime-completion:1.3:1.3.8
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Split `state.rs` and guard native-only host operations
  category: refactor
  task_type: refactor
  depends_on: []
  validation_criteria: '2.1.1: `kill_group` is the only `killpg` call in `crates/gterminal/src/host/`
    and returns `InvalidPgid` for `pgid <= 0` without calling into libc. test: `crates/gterminal/src/host/native_ops/tests.rs::kill_group_refuses_non_positive_pgid`.

    2.1.2: `kill`, `write`, `write_paste`, `resize`, `snapshot`, `reserve_observer`,
    and `release_observer` against a tmux observer slot each answer `not_native`,
    the host process group and observer table are untouched, and `ping` keeps answering.
    test: `crates/gterminal/tests/control_protocol.rs::native_verbs_refuse_tmux_terminals`.

    2.1.3: A control line of `MAX_CONTROL_LINE + 1` bytes is answered `control_overflow`
    and the connection closes without parsing; a peer that sends more than the cap
    with no newline is disconnected with the same error and bounded memory. test:
    `crates/gterminal/tests/control_protocol.rs::oversize_control_line_is_refused_before_parse`.

    2.1.4: `host/state.rs` is under 600 lines and every `crates/gterminal/src` file
    stays under the ceiling. test: `crates/gterminal/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.

    2.1.5: Dropping the frame sender ends the broadcast task within one ticker interval.
    test: `crates/gterminal/tests/frame_producer.rs::broadcast_task_exits_on_closed_channel`.

    2.1.6: Snapshot truncation of multibyte scrollback never panics and returns valid
    UTF-8 within the byte cap with the truncation flag set. test: `crates/gterminal/tests/control_protocol.rs::snapshot_truncates_on_char_boundaries`.

    2.1.7: After an observer is reaped its frame connection receives a typed `observer_reaped`
    close and the task exits instead of spinning. test: `crates/gterminal/tests/embed.rs::reaped_observer_frame_task_exits`.

    2.1.8: Guard set G groups 3, 4, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:2.1:2.1.1
  - covers:native-runtime-completion:2.1:2.1.2
  - covers:native-runtime-completion:2.1:2.1.3
  - covers:native-runtime-completion:2.1:2.1.4
  - covers:native-runtime-completion:2.1:2.1.5
  - covers:native-runtime-completion:2.1:2.1.6
  - covers:native-runtime-completion:2.1:2.1.7
  - covers:native-runtime-completion:2.1:2.1.8
  tdd: false
  source_section: '2.1'
  assigned_agent: backend-developer
- title: Replace the FIFO gate with a `gterm gate` exec-status barrier
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '1.3'
  validation_criteria: '2.2.1: Committing a prepared spawn whose argv names a missing
    binary answers `exec_failed` with `code="ENOENT"`, a file without the execute
    bit `code="EACCES"`, and an executable text file with no recognized header `code="ENOEXEC"`
    (no shell is ever started for it), each with `detail` and `stage:"execve"`, and
    no prepared or committed slot survives in `list`. test: `crates/gterminal/tests/host_lifecycle.rs::commit_reports_exec_failure_with_errno`.

    2.2.2: A gate child that never execs answers `exec_timeout` after `commit_deadline_ms`
    and is killed; a truncated status answers `malformed_status`; both leave no slot
    in `list`. test: `crates/gterminal/tests/host_lifecycle.rs::commit_times_out_and_rejects_malformed_status`.

    2.2.3: A successful commit returns only after exec (the child''s process name
    equals argv[0]) and `spawn.rs` no longer references `mkfifo` or `/bin/sh`. test:
    `crates/gterminal/tests/host_lifecycle.rs::commit_returns_after_exec`.

    2.2.4: `NativeTerminalRuntime.commit_spawn` forwards `commit_deadline_ms` from
    config and maps `exec_failed` to `SpawnResult.error="exec_failed:<code>"` for
    errno codes, `exec_timeout` to `"exec_timeout"`, and `malformed_status` to `"malformed_status"`,
    with `error_detail` from the host''s `detail` and `stage`. test: `tests/agents/test_native_spawn.py::test_commit_forwards_deadline_and_maps_exec_failed`.

    2.2.5: A gate child killed after the gate byte and before `execve` (fault injected
    through a `GTERM_GATE_FAULT` environment value the gate honours only under `cfg(test)`-built
    helpers) is answered `committed` and then settled as an exit by that signal with
    no prepared slot left; a fault injected at `setsid`, `dup2`, or `PATH` resolution
    answers `exec_failed` with that `stage` and no slot survives. test: `crates/gterminal/tests/host_lifecycle.rs::preexec_signal_settles_as_exit_and_injected_faults_never_commit`.

    2.2.8: A committed `/usr/bin/true` that exits before the host reads fd 4 is answered
    `committed` and settles exited with status 0, and `/usr/bin/false` settles with
    status 1; neither is answered `exec_failed`, and after settlement no slot survives
    in `list`. test: `crates/gterminal/tests/host_lifecycle.rs::fast_exit_after_exec_is_committed_then_exited`.

    2.2.6: Killing the host while a prepared child is waiting makes the child exit
    within 1 s on gate-pipe EOF, leaving no blocked child behind. test: `crates/gterminal/tests/host_lifecycle.rs::host_death_releases_prepared_child`.

    2.2.7: Guard set G groups 1, 2, 3, 5, and 7 pass from the `0.5.0` checkout with
    the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:2.2:2.2.1
  - covers:native-runtime-completion:2.2:2.2.2
  - covers:native-runtime-completion:2.2:2.2.3
  - covers:native-runtime-completion:2.2:2.2.4
  - covers:native-runtime-completion:2.2:2.2.5
  - covers:native-runtime-completion:2.2:2.2.8
  - covers:native-runtime-completion:2.2:2.2.6
  - covers:native-runtime-completion:2.2:2.2.7
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Real host drain, daemon shutdown escalation, and test process hygiene
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '3.1.1: Removing the socket directory makes the host exit within
    two health ticks with reason `socket_dir_removed`. test: `crates/gterminal/tests/host_lifecycle.rs::host_exits_when_socket_dir_vanishes`.

    3.1.2: `host_shutdown{grace_ms:500}` SIGHUPs a child that traps SIGHUP, waits
    about 500 ms, then SIGKILLs it; a child that exits on SIGHUP is not SIGKILLed.
    test: `crates/gterminal/tests/host_lifecycle.rs::drain_honours_grace_then_kills`.

    3.1.3: The control protocol has no `attach` verb and the fake host answers `unknown_verb`
    for it. test: `tests/terminals/test_host_manager.py::test_fake_host_has_no_attach_verb`.

    3.1.4: `_host_shutdown` escalates RPC, SIGTERM, SIGKILL, each after `shutdown_grace_seconds`,
    and stops at the first rung that ends the process. test: `tests/terminals/test_host_manager.py::test_host_shutdown_escalates`.

    3.1.5: The `tests/terminals` session leaves no `gterm host` process that it created,
    and every Rust host test owns its host through `HostProc`. test: `tests/terminals/conftest.py::_assert_no_leaked_hosts`.
    symbol: `HostProc`.

    3.1.6: Guard set G groups 2, 3, 5, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:3.1:3.1.1
  - covers:native-runtime-completion:3.1:3.1.2
  - covers:native-runtime-completion:3.1:3.1.3
  - covers:native-runtime-completion:3.1:3.1.4
  - covers:native-runtime-completion:3.1:3.1.5
  - covers:native-runtime-completion:3.1:3.1.6
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Request correlation, single reader, event stream, and native exit events
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '3.1'
  - '1.3'
  validation_criteria: '3.2.1: A request without `id` is answered `missing_id`; two
    in-flight requests with one id get `duplicate_id` on the second; every response
    echoes `id`. test: `crates/gterminal/tests/control_protocol.rs::requests_require_unique_ids`.

    3.2.2: A committed child that exits produces exactly one `terminal_exited` event
    with its `exit_code` on the event stream, including a child already reaped when
    commit answered, and the request writer never carries events. test: `crates/gterminal/tests/control_protocol.rs::child_exit_emits_terminal_exited_on_event_stream`.

    3.2.3: `HostClient` serves interleaved responses to concurrent callers by id through
    one reader task, and a broken connection resolves every pending call with `HostConnectionLost`.
    test: `tests/terminals/test_host_client.py::test_reader_task_correlates_and_fails_pending_on_loss`.

    3.2.4: After an event gap the manager lists the host, promotes a `commit_indeterminate`
    row that is listed committed, kills a listed-but-prepared slot by its host id
    and fails the row `not_committed`, and fails an absent one `not_found`. test:
    `tests/terminals/test_host_manager.py::test_gap_settles_indeterminate_from_list`.

    3.2.5: `is_live` returns `False` for a native row without `host_epoch`; no `_frame_host_epoch`
    attribute exists; `resize` and `terminate` reconnect with the manager''s socket
    path and the row''s `host_epoch`, and a row from an earlier epoch is refused `host_epoch_changed`
    with no request sent. test: `tests/agents/test_native_spawn.py::test_is_live_requires_epoch_and_reconnect_has_arguments`.

    3.2.6: `reconcile_host_inventory` lets a non-`ReconcileError` exception propagate
    and reaping never blocks the loop for longer than one scheduler tick. test: `tests/terminals/test_host_manager.py::test_reconcile_catches_only_typed_errors`,
    `tests/terminals/test_host_manager.py::test_reap_runs_off_loop`.

    3.2.7: Wire goldens match the Rust emitter for `id`, `since`, `gap`, and `terminal_exited`,
    and the terminal wiring is built once in `terminal_wiring.py`. test: `tests/terminals/test_wire_golden.py::test_control_goldens_match_rust_emitter`,
    `tests/terminals/test_composition_roots.py::test_orchestration_builds_terminal_services_once`.

    3.2.8: With event production faster than the ring capacity throughout recovery,
    one subscribe-before-list cycle converges, delivers every event after the list
    cut once and in order, and never enters a second gap. test: `tests/terminals/test_host_manager.py::test_gap_recovery_converges_under_ring_churn`.

    3.2.9: `reconnect` and `close` fail old pending calls once, admit no stale reply
    into the replacement reader generation, and leave zero old reader tasks; cancelling
    one caller leaves unrelated futures untouched. test: `tests/terminals/test_host_client.py::test_reconnect_replaces_reader_generation_atomically`.

    3.2.10: After a host crash the event reader joins the manager''s single restart,
    and during drain or stop it exits on `HostManagerStopped` without moving its cursor
    or spawning a host. test: `tests/terminals/test_host_manager.py::test_event_reader_joins_singleflight_and_stops_cleanly`.

    3.2.11: A 1 MiB control reply is decoded on the initial connection, on a reconnect
    replacement, and on the event stream, and a line over `MAX_CONTROL_LINE` resolves
    every pending call with `HostConnectionLost`; `readline` is never called with
    a `limit` argument. test: `tests/terminals/test_host_client.py::test_reader_decodes_large_reply_and_fails_over_cap`.

    3.2.12: While a `spawn_commit` on one connection waits on its deadline, `ping`,
    `list`, `resize`, and `kill` on the same connection are answered, responses carry
    their own ids, the ledger stays totally ordered, and the 65th in-flight request
    is refused `too_many_inflight`. test: `crates/gterminal/tests/control_protocol.rs::commit_wait_does_not_block_other_requests`.

    3.2.13: With the gap buffer overflowing before the list reply, the manager discards
    the buffer, repeats the cut after the reply, converges without applying any stale
    event, and ends subscribed at the final list cursor. test: `tests/terminals/test_host_manager.py::test_gap_buffer_overflow_repeats_cut`.

    3.2.14: A tmux observer whose pane dies emits no control-plane event while a control
    subscriber is attached; the exit reaches only the slot''s frame stream. test:
    `crates/gterminal/tests/control_protocol.rs::tmux_pane_death_emits_no_control_event`.

    3.2.15: Guard set G groups 1, 2, 3, 5, and 7 pass from the `0.5.0` checkout with
    the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:3.2:3.2.1
  - covers:native-runtime-completion:3.2:3.2.2
  - covers:native-runtime-completion:3.2:3.2.3
  - covers:native-runtime-completion:3.2:3.2.4
  - covers:native-runtime-completion:3.2:3.2.5
  - covers:native-runtime-completion:3.2:3.2.6
  - covers:native-runtime-completion:3.2:3.2.7
  - covers:native-runtime-completion:3.2:3.2.8
  - covers:native-runtime-completion:3.2:3.2.9
  - covers:native-runtime-completion:3.2:3.2.10
  - covers:native-runtime-completion:3.2:3.2.11
  - covers:native-runtime-completion:3.2:3.2.12
  - covers:native-runtime-completion:3.2:3.2.13
  - covers:native-runtime-completion:3.2:3.2.14
  - covers:native-runtime-completion:3.2:3.2.15
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Vendor build gating and release assertions
  category: config
  task_type: task
  depends_on: []
  validation_criteria: '4.1.1: Without `GOBBY_RUN_VENDOR_BUILD` the vendor suite collects
    and reports its zig cases as skipped, never failed. test: `tests/gterminal/test_vendor_layer.py::test_zig_cases_skip_without_opt_in`.

    4.1.2: The release workflow asserts `NOTICE.md` and no longer references `portable-pty/LICENSE.md`.
    file: `.github/workflows/release-gterminal.yml`.

    4.1.3: Every path the release workflow''s package assertion requires is present
    in `cargo package -p gobby-terminal --list --no-verify`, exercised by a test that
    runs the same loop against the real listing. test: `tests/gterminal/test_release_assertions.py::test_release_assertion_paths_are_packaged`.

    4.1.4: `cargo package --no-verify` for `gobby-terminal` and `gobby-client` emits
    no licence warning. test: `tests/gterminal/test_release_assertions.py::test_package_emits_no_license_warning`.

    4.1.5: Guard set G groups 1 and 5 pass from the `0.5.0` checkout with the isolated
    test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set
    G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:4.1:4.1.1
  - covers:native-runtime-completion:4.1:4.1.2
  - covers:native-runtime-completion:4.1:4.1.3
  - covers:native-runtime-completion:4.1:4.1.4
  - covers:native-runtime-completion:4.1:4.1.5
  tdd: true
  source_section: '4.1'
  assigned_agent: backend-developer
- title: Lint policy without blanket allows
  category: refactor
  task_type: refactor
  depends_on:
  - '2.1'
  - '3.1'
  validation_criteria: '4.2.1: `cargo clippy -p gobby-terminal --all-targets --features
    vt-engine -- -D warnings` passes with no `clippy::all` or `dead_code` allow outside
    `src/ghostty/` and every remaining allow carrying a `// reason:` line, and the
    landed Windows cross-target clippy job in `rust-ci.yml` stays green. test: `crates/gterminal/tests/carve_guard.rs::no_blanket_lint_allows`.

    4.2.3: `platform/macos.rs` and `platform/windows.rs` are each under 700 production
    lines, `platform/macos_process.rs` owns the macOS process inspection helpers,
    and `platform/windows_daemon.rs` owns the Windows daemon launch helpers. file:
    `crates/gterminal/src/platform/macos_process.rs`. file: `crates/gterminal/src/platform/windows_daemon.rs`.
    test: `crates/gterminal/tests/source_size.rs::no_src_file_at_or_above_1000_lines`.

    4.2.2: Guard set G groups 3, 4, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:4.2:4.2.1
  - covers:native-runtime-completion:4.2:4.2.3
  - covers:native-runtime-completion:4.2:4.2.2
  tdd: false
  source_section: '4.2'
  assigned_agent: backend-developer
- title: nextest PTY group filter and private build env
  category: config
  task_type: task
  depends_on: []
  validation_criteria: '4.3.1: `cargo nextest list -p gobby-terminal --features vt-engine`
    places every `host_lifecycle`, `control_protocol`, `frame_producer`, `frame_protocol`,
    and `embed` test in the `gterm-pty` group. file: `.config/nextest.toml`.

    4.3.2: Test-driven cargo builds use a target dir outside the workspace `target/`.
    test: `crates/gterminal/tests/build_env.rs::build_env_uses_private_target_dir`.

    4.3.3: Guard set G groups 3 and 7 pass from the `0.5.0` checkout with the isolated
    test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set
    G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:4.3:4.3.1
  - covers:native-runtime-completion:4.3:4.3.2
  - covers:native-runtime-completion:4.3:4.3.3
  tdd: true
  source_section: '4.3'
  assigned_agent: backend-developer
- title: Unpublished managed binaries in the installer
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '4.4.1: `is_published("gterm")` is `False` while `gterm` is
    in `UNPUBLISHED_MANAGED_BINS`, and the installer never calls the GitHub, binstall,
    or cargo-install fetchers for an unpublished binary. test: `tests/cli/test_install_setup_gterm.py::test_unpublished_binary_skips_remote_fetchers`.

    4.4.2: Table row `unpublished, absent`: with a workspace checkout the source build
    runs and is stamped; without one `ManagedBinaryReleaseMissing` is raised naming
    the binary, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_unpublished_absent_binary_builds_or_raises`,
    `tests/cli/test_install_setup_gclient.py::test_unpublished_absent_binary_builds_or_raises`.

    4.4.3: Table row `unpublished, present, force=False`: the binary is kept and stamped
    with `method="local"` regardless of version, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_unpublished_present_binary_is_kept`,
    `tests/cli/test_install_setup_gclient.py::test_unpublished_present_binary_is_kept`.

    4.4.4: Table row `published, present, satisfying, force=False`: the binary is
    kept and stamped without fetching, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_published_satisfying_binary_is_kept`,
    `tests/cli/test_install_setup_gclient.py::test_published_satisfying_binary_is_kept`.

    4.4.5: Table row `published, present, not satisfying`: the fetch chain runs in
    order github, binstall, cargo install, cargo git, stopping at the first success,
    for both installers. test: `tests/cli/test_install_setup_gterm.py::test_published_binary_runs_fetch_chain`,
    `tests/cli/test_install_setup_gclient.py::test_published_binary_runs_fetch_chain`.

    4.4.6: Table row `published, absent`: the same ordered fetch chain runs for both
    installers, stopping at the first success. test: `tests/cli/test_install_setup_gterm.py::test_published_absent_binary_runs_fetch_chain`,
    `tests/cli/test_install_setup_gclient.py::test_published_absent_binary_runs_fetch_chain`.

    4.4.7: Table row `unpublished, present, force=True`: the present binary is not
    kept; the source build runs, else `ManagedBinaryReleaseMissing`, for both installers.
    test: `tests/cli/test_install_setup_gterm.py::test_force_rebuilds_unpublished_present_binary`,
    `tests/cli/test_install_setup_gclient.py::test_force_rebuilds_unpublished_present_binary`.

    4.4.8: Table row `published, present, satisfying, force=True`: the present binary
    is not kept and the fetch chain runs, for both installers. test: `tests/cli/test_install_setup_gterm.py::test_force_refetches_published_satisfying_binary`,
    `tests/cli/test_install_setup_gclient.py::test_force_refetches_published_satisfying_binary`.

    4.4.9: Guard set G groups 1, 5, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:4.4:4.4.1
  - covers:native-runtime-completion:4.4:4.4.2
  - covers:native-runtime-completion:4.4:4.4.3
  - covers:native-runtime-completion:4.4:4.4.4
  - covers:native-runtime-completion:4.4:4.4.5
  - covers:native-runtime-completion:4.4:4.4.6
  - covers:native-runtime-completion:4.4:4.4.7
  - covers:native-runtime-completion:4.4:4.4.8
  - covers:native-runtime-completion:4.4:4.4.9
  tdd: true
  source_section: '4.4'
  implementation_domain: backend
- title: Single lease registry owning locks and leases
  category: refactor
  task_type: refactor
  depends_on:
  - '1.3'
  - '3.2'
  validation_criteria: '5.1.1: The container, coordinator, and WebSocket mixin share
    one `TerminalLeaseRegistry` instance, and `WriteCoordinator` has no `_leases`,
    `grant_lease`, `takeover_lease`, or `_locks`. test: `tests/terminals/test_composition_roots.py::test_single_lease_registry_is_injected`.

    5.1.2: A write whose lease was taken over between admission and persist is refused
    `lease_lost` and nothing is persisted. test: `tests/terminals/test_write_coordinator.py::test_revalidate_before_persist`.

    5.1.3: Concurrent `take_control` calls on one terminal serialize under `registry.lock(terminal_id)`
    and the cell is released when the last holder exits. test: `tests/terminals/test_lease_authority.py::test_lock_cells_are_refcounted`.

    5.1.4: `TerminalManager.clear_orphaned_attachment_writes(machine_id, daemon_epoch)`
    deletes persisted `ws:` latches whose stored `daemon_epoch` is an earlier epoch
    on this machine''s rows, leaves the current epoch''s and other machines'' entries,
    and does so after a real store round trip through PostgreSQL and `MemoryTerminalStore`
    with the entries written by `persist_unresolved_write`. test: `tests/storage/test_terminals.py::test_orphan_sweep_clears_only_dead_epochs_on_this_machine`.

    5.1.12: `persist_unresolved_write` stores `daemon_epoch` on every entry beside
    `at` and `origin`, raises `ValueError` without it, and the coordinator passes
    the epoch it was constructed with on every operator latch. test: `tests/storage/test_terminals.py::test_latch_entries_carry_daemon_epoch`,
    `tests/terminals/test_write_coordinator.py::test_operator_latch_carries_daemon_epoch`.

    5.1.13: With a resolved runtime paused mid-dispatch, an `input`, `paste`, and
    `text` operator write each reach the runtime only through `WriteCoordinator.write`,
    a takeover, release, finalize, or exit cleanup issued meanwhile waits behind the
    write, and `terminal_ws.py` contains no direct `write_input`, `write_paste`, or
    `write_text` call. test: `tests/servers/test_terminal_ws_input.py::test_operator_writes_route_through_coordinator`.

    5.1.10: `terminal_wiring.py` runs the orphan sweep once, after the manager is
    built and before the registry or coordinator is handed to any consumer, and constructs
    exactly one registry. test: `tests/terminals/test_composition_roots.py::test_wiring_sweeps_orphans_before_accepting_writes`.

    5.1.5: An operator write without `attachment_id` is answered `attachment_required`;
    an unresolvable runtime is answered `runtime_unavailable`; `terminal_ws.py` imports
    nothing from `unittest.mock`. test: `tests/servers/test_terminal_ws_lease.py::test_attachment_required_and_runtime_unavailable`.

    5.1.6: `lifecycle_monitor.py` and `terminal_ws.py` are each under 950 lines, the
    monitor builds its terminal services through `build_terminal_services`, and take
    and release control are served from `TerminalControlMixin`. symbol: `build_terminal_services`.
    file: `src/gobby/agents/lifecycle_monitor_terminals.py`. file: `src/gobby/servers/websocket/terminal_ws_control.py`.

    5.1.7: Every production and direct-test caller awaits the async lease mutations,
    and the focused suites emit no un-awaited-coroutine warning. test: `tests/servers/test_tmux_bridge_authority.py::test_async_lease_mutation_callers_are_awaited`.

    5.1.8: With `frame.close()` paused, an input racing either proxy-relay cleanup
    path is refused stale and reaches no runtime; both paths revoke authority before
    frame teardown. test: `tests/servers/test_terminal_ws_lease.py::test_finalize_revokes_authority_before_frame_close`.

    5.1.9: Takeover, release, finalize, and exit cleanup each wait behind an in-flight
    write and preserve its latch until settlement; finalize followed by re-attach
    reuses one lock cell, and cancelling a queued waiter releases only its borrow.
    test: `tests/terminals/test_write_coordinator.py::test_lease_mutations_linearize_with_dispatch`.

    5.1.11: Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with
    the isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:5.1:5.1.1
  - covers:native-runtime-completion:5.1:5.1.2
  - covers:native-runtime-completion:5.1:5.1.3
  - covers:native-runtime-completion:5.1:5.1.4
  - covers:native-runtime-completion:5.1:5.1.12
  - covers:native-runtime-completion:5.1:5.1.13
  - covers:native-runtime-completion:5.1:5.1.10
  - covers:native-runtime-completion:5.1:5.1.5
  - covers:native-runtime-completion:5.1:5.1.6
  - covers:native-runtime-completion:5.1:5.1.7
  - covers:native-runtime-completion:5.1:5.1.8
  - covers:native-runtime-completion:5.1:5.1.9
  - covers:native-runtime-completion:5.1:5.1.11
  tdd: false
  source_section: '5.1'
  assigned_agent: backend-developer
- title: '`send_keys` and attention respond through the coordinator'
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: '5.2.1: `send_keys` against a terminal with a live operator
    lease is delivered with `origin="daemon"` instead of refused stale, and a same-key
    replay returns the recorded outcome. test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_keys_uses_daemon_origin_and_idempotency`.

    5.2.2: A same-key replay with a different payload fingerprint is refused `idempotency_conflict`.
    test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_keys_idempotency_conflict`.

    5.2.3: Attention `respond` writes through the coordinator with `origin="attention"`
    and refuses when the entry''s state changed after the caller read it. test: `tests/servers/test_attention_respond.py::test_respond_routes_through_coordinator_with_cas`.

    5.2.4: An explicit-key retry of an unresolved write dispatches nothing to the
    runtime and returns the stored indeterminate outcome with the same key; reuse
    of the key after a delivered outcome is a fresh write; a malformed key is a typed
    refusal; the no-key form returns a fresh key whose retry behaves the same way.
    test: `tests/mcp_proxy/test_sessions_terminal_tools.py::test_send_keys_idempotency_key_contract`.

    5.2.5: Payload fingerprints round-trip through PostgreSQL and `MemoryTerminalStore`
    and count against the existing unresolved-write entry and byte caps. test: `tests/storage/test_terminals.py::test_unresolved_write_fingerprint_round_trip`.

    5.2.6: Guard set G groups 1, 2, 5, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior:
    "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:5.2:5.2.1
  - covers:native-runtime-completion:5.2:5.2.2
  - covers:native-runtime-completion:5.2:5.2.3
  - covers:native-runtime-completion:5.2:5.2.4
  - covers:native-runtime-completion:5.2:5.2.5
  - covers:native-runtime-completion:5.2:5.2.6
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: Control lease lifecycle and write settlement in the web client
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: "6.1.1: Focusing a terminal sends one `terminal_take_control`,\
    \ a keystroke before `control_result` is held in the single pending slot and sent\
    \ after, and blur sends `terminal_release_control`. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::takes\
    \ and releases control around focus`.\n6.1.2: The reducer clears on `delivered`,\
    \ discards on `refused`, and on `indeterminate` exposes exactly one retry that\
    \ resends the same seq and payload. test: `web/src/hooks/__tests__/terminalWriteSettlement.test.ts::settles\
    \ outcomes without auto-resend`.\n6.1.3: After `lease_lost` the view is read-only\
    \ and typing does not send input until control is retaken; paste sends `terminal_paste`\
    \ with the clipboard text. test: `web/tests/terminal-control-lease.spec.ts::lease\
    \ loss goes read-only and paste is bracketed`.\n6.1.4: Reordering the roster does\
    \ not remount the terminal component for an unchanged `terminal_id`. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::keeps\
    \ terminal mounts stable across roster changes`.\n6.1.5: A second keystroke while\
    \ `pendingInput` is occupied is refused with the visible cue and never sent; `granted:false`,\
    \ request timeout, `lease_lost`, blur, and reconnect each discard the slot with\
    \ a visible refusal and clear the in-flight control request, and a `control_result`\
    \ arriving after that clear grants nothing. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::refuses\
    \ extra input and clears the pending slot on every failure path`.\n6.1.6: Take-back,\
    \ retry, discard, and jump-to-bottom are reachable by keyboard with visible focus,\
    \ expose non-colour state cues, and measure at least 44\xD744 at the 440\xD7956\
    \ tier. test: `web/tests/terminal-control-lease.spec.ts::lease controls are keyboard\
    \ operable and sized for touch`.\n6.1.7: Guard set G group 6 passes from the `0.5.0`\
    \ checkout, plus `npx playwright test tests/terminal-control-lease.spec.ts` at\
    \ the three tiers. behavior: \"Guard set G\" in `docs/guides/gterminal-development-guide.md`."
  labels:
  - covers:native-runtime-completion:6.1:6.1.1
  - covers:native-runtime-completion:6.1:6.1.2
  - covers:native-runtime-completion:6.1:6.1.3
  - covers:native-runtime-completion:6.1:6.1.4
  - covers:native-runtime-completion:6.1:6.1.5
  - covers:native-runtime-completion:6.1:6.1.6
  - covers:native-runtime-completion:6.1:6.1.7
  tdd: true
  source_section: '6.1'
  implementation_domain: frontend
- title: Fragment accumulation, readiness rendezvous, and bounded history
  category: code
  task_type: feature
  depends_on:
  - '6.1'
  validation_criteria: "6.2.1: Fragments flush once per tick and exceeding the 256\
    \ KiB budget (256 B overhead per fragment) triggers exactly one `refresh` whose\
    \ snapshot is pinned. test: `web/src/hooks/__tests__/terminalWsFragments.test.ts::charges\
    \ overhead and refreshes over budget`.\n6.2.2: `setViewport` is not sent before\
    \ the attachment reports ready and is sent once after. test: `web/src/hooks/__tests__/terminalAttachmentReadiness.test.ts::defers\
    \ viewport until ready`.\n6.2.3: History is replaced when the attachment changes\
    \ and never exceeds 2 000 lines or 256 KiB at 440\xD7956, 932\xD7430, and 1440\xD7\
    900; at the two mobile tiers a line above the initial viewport becomes visible\
    \ by touch scroll and live output does not snap the view back. test: `web/tests/terminal-history-scroll.spec.ts::bounds\
    \ history per attachment across tiers`.\n6.2.4: A reconnect that installs a replacement\
    \ attachment whose history overlaps the rendered tail keeps the same component\
    \ instance, renders each numbered line exactly once in order, and derives the\
    \ scroll position from the new window. test: `web/tests/terminal-history-scroll.spec.ts::replacement\
    \ attachment resets history without remount`.\n6.2.5: Guard set G group 6 passes\
    \ from the `0.5.0` checkout, plus `npx playwright test tests/terminal-history-scroll.spec.ts`\
    \ at the three tiers. behavior: \"Guard set G\" in `docs/guides/gterminal-development-guide.md`."
  labels:
  - covers:native-runtime-completion:6.2:6.2.1
  - covers:native-runtime-completion:6.2:6.2.2
  - covers:native-runtime-completion:6.2:6.2.3
  - covers:native-runtime-completion:6.2:6.2.4
  - covers:native-runtime-completion:6.2:6.2.5
  tdd: true
  source_section: '6.2'
  implementation_domain: frontend
- title: Host backpressure module
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '7.1.1: A frame peer that exceeds `delta_queue_bytes` but drains
    before `lag_timeout` never holds more than the byte cap, receives exactly one
    replacement keyframe, stays connected, and does not slow a continuously reading
    observer. test: `crates/gterminal/tests/frame_producer.rs::slow_observer_resyncs_with_one_keyframe`.

    7.1.2: A frame peer that never drains is closed `lagged` at the configured timeout
    and its observer slot is released, while another observer keeps receiving frames.
    test: `crates/gterminal/tests/frame_producer.rs::lagged_observer_is_closed_and_released`.

    7.1.3: Control responses are not coalesced below the queue cap; a non-reading
    control peer is closed at `control_deadline`, an overflowing event subscriber
    receives `event_overflow`, and the shipped defaults are 5 s and 2 s. test: `crates/gterminal/src/host/backpressure/tests.rs::control_deadline_and_event_overflow`.

    7.1.4: Guard set G groups 3 and 7 pass from the `0.5.0` checkout with the isolated
    test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set
    G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:7.1:7.1.1
  - covers:native-runtime-completion:7.1:7.1.2
  - covers:native-runtime-completion:7.1:7.1.3
  - covers:native-runtime-completion:7.1:7.1.4
  tdd: true
  source_section: '7.1'
  implementation_domain: backend
- title: Host-driven acceptance tests
  category: test
  task_type: task
  depends_on:
  - '3.2'
  - '5.2'
  - '7.1'
  validation_criteria: '7.2.1: The acceptance package covers spawn, commit, and exit
    settlement, exec failure, host crash and respawn, drain, lease takeover and write
    settlement, `send_keys` idempotency, and the tmux parity list, with a header table
    in each module mapping every test to the plan item it proves. file: `tests/terminals/acceptance/test_native_lifecycle.py`.
    file: `tests/terminals/acceptance/test_native_writes.py`. file: `tests/terminals/acceptance/test_tmux_parity.py`.

    7.2.2: The suite passes against a freshly built host on macOS and Linux in the
    `terminal-acceptance` job. file: `.github/workflows/ci.yml`.

    7.2.3: `list` at the shipped ceilings (124 native terminals plus 64 tmux observers,
    each with a 1 024-byte title and maximum-size bounded fields) from a live host
    stays under the control line cap, and the 189th admission is refused `capacity`.
    test: `crates/gterminal/tests/control_protocol.rs::list_envelope_fits_under_line_cap`.

    7.2.4: Guard set G groups 2, 3, and 7 pass from the `0.5.0` checkout with the
    isolated test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out, plus `GOBBY_RUN_VENDOR_BUILD=1
    uv run pytest tests/terminals/acceptance`. behavior: "Guard set G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:7.2:7.2.1
  - covers:native-runtime-completion:7.2:7.2.2
  - covers:native-runtime-completion:7.2:7.2.3
  - covers:native-runtime-completion:7.2:7.2.4
  tdd: false
  source_section: '7.2'
  assigned_agent: backend-developer
- title: Flip evidence document and checker
  category: test
  task_type: task
  depends_on: []
  validation_criteria: '8.1.1: With `default_backend="tmux"` the checker passes on
    the stub; with `"native"` it fails on the stub, fails when only one OS is green,
    and passes with both OSes green at one commit. test: `tests/config/test_native_backend_flip.py::test_flip_gate_rules`.

    8.1.2: The checked-in default is `tmux`, the evidence document exists, and the
    dev guide''s "Backend status" section names the gate and the rollback. symbol:
    `TerminalConfig`. file: `docs/evidence/native-backend-flip.md`. behavior: "Backend
    status" in `docs/guides/gterminal-development-guide.md`.

    8.1.3: A later red row after a qualifying same-commit macOS/Linux green pair causes
    the checker to fail under the documented row ordering. test: `tests/config/test_native_backend_flip.py::test_flip_gate_rules`.

    8.1.4: Guard set G groups 1 and 5 pass from the `0.5.0` checkout with the isolated
    test hub DSN and `GOBBY_TEST_PROTECT=1`, with no carve-out. behavior: "Guard set
    G" in `docs/guides/gterminal-development-guide.md`.'
  labels:
  - covers:native-runtime-completion:8.1:8.1.1
  - covers:native-runtime-completion:8.1:8.1.2
  - covers:native-runtime-completion:8.1:8.1.3
  - covers:native-runtime-completion:8.1:8.1.4
  tdd: false
  source_section: '8.1'
  assigned_agent: backend-developer
```
