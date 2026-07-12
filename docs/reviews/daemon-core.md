# Review: daemon core (runner*, system_automation, gwiki_gateway, app_context, shutdown_intent, paths)

- **Scope:** `src/gobby/runner.py`, `runner_lifecycle.py`, `runner_lifecycle_startup.py`, `runner_lifecycle_subsystems.py`, `runner_lifecycle_periodic.py`, `runner_lifecycle_shutdown.py`, `runner_lifecycle_agents.py`, `runner_maintenance.py`, `runner_broadcasting.py`, `system_automation.py`, `gwiki_gateway.py`, `app_context.py`, `shutdown_intent.py`, `paths.py`, `__init__.py`, `postgres_pgsearch_assets.py` (~5,250 lines)
- **Reviewer:** Claude Fable 5 — 6-agent fan-out (entry/startup, shutdown/intent, wiring/context, maintenance/periodic, automation/agents, gateway/broadcast/paths) + synthesizer verification of every Blocker against source
- **Commit / branch:** `76beca60b` / `0.5.0`
- **Summary:** 3 Blocker · 26 Important · 18 Nit — the lifecycle is feature-rich but its failure semantics are accidental: boot and teardown each abort on a single unexpected exception, periodic jobs can die silently before their first tick, destructive git runs in an arbitrary cwd, and startup-CWD project pinning leaks into machine-global surfaces.

## Findings

### [BLOCKER] Expired-isolation reaper runs destructive git commands in an arbitrary daemon cwd

- **Where:** `src/gobby/runner_maintenance.py:575-592` (call sites inside `cleanup_expired_isolation_loop`), `src/gobby/runner_maintenance.py:738-743` (`_run_git_command` — `subprocess.run(args, capture_output=True, timeout=30)`, no `cwd`)
- **Failure mode:** The reaper runs `git worktree remove --force <path>`, `git worktree prune`, and `git branch -D <wt.branch_name>` with whatever cwd the daemon inherited. The daemon spawn (`src/gobby/cli/daemon.py:484`) sets no `cwd`, so it is wherever `gobby start` ran. Two outcomes: (a) cwd is not the worktree's parent repo → `worktree remove` fails, the `shutil.rmtree` fallback deletes the directory, but `prune` and `branch -D` silently no-op in the wrong place — the real parent repo accumulates stale `.git/worktrees/` admin entries forever and orphaned branches are never deleted, so a later `git worktree add` for the same path/branch fails; (b) cwd happens to be a *different* git repo containing a branch with the same name → `git branch -D` force-deletes that unrelated repo's branch. The `Worktree` model carries `project_id` but the loop never resolves a repo path; the return codes of `prune` and `branch -D` are discarded.
- **Why it matters:** Wrong-repo `branch -D` is unrecoverable loss of unmerged work; the silent-failure path is a permanent resource leak that breaks future worktree creation. One daemon serves many projects, so "cwd == correct repo" is true for at most one of them.
- **Minimal fix:** Resolve the parent repo per record (project path from `project_id`, or parse the worktree's `.git` file before deleting it) and run every git command as `["git", "-C", repo_path, ...]`; check and log the currently ignored return codes.
- **Confidence:** high — synthesizer-verified: `_run_git_command` source has no `cwd`, the loop builds all three commands without `-C`, and the daemon `Popen` sets no cwd.

### [BLOCKER] Lost port-bind race destroys the live daemon's PID record and skips all cleanup

- **Where:** `src/gobby/runner_lifecycle.py:122-127` (unconditional PID overwrite), `:148` (`server_task = asyncio.create_task(server.serve())`), `:191-194` (`except Exception` cleanup path); `src/gobby/runner.py:248-266` (health-check guard + exception handling); `src/gobby/runner_maintenance.py:837-847` (`cleanup_pid_file`)
- **Failure mode:** The only double-start guard in `main()` is `_healthy_daemon_running()` (runner.py:253), which returns False for the entire window from process start until uvicorn binds — a window that includes the whole synchronous 4-phase `GobbyRunner.__init__` (Postgres connect etc.). A second instance (launchd respawn, manual start) passing that guard blindly overwrites `gobby.pid` (runner_lifecycle.py:124) — no `O_EXCL`, no flock, no liveness check. It then loses the bind race: uvicorn 0.40.0's `Server.startup()` calls `sys.exit(1)` on `OSError` (verified in the installed package). A `SystemExit` raised inside the unobserved server task propagates out of `asyncio.run()` cancelling `run_daemon` with `CancelledError` (verified empirically on Python 3.14.3) — caught by **neither** `except Exception` at runner_lifecycle.py:191 **nor** runner.py:264, since `SystemExit`/`CancelledError` derive from `BaseException`, not `Exception`. So `cleanup_pid_file()` never runs and nothing already started is torn down. The PID file now holds the dead loser's PID; and had cleanup run, `cleanup_pid_file` would *unlink* the file (stored PID == loser's own) — the winner's record is destroyed either way.
- **Why it matters:** `gobby stop`/`restart`/`status` resolve the daemon via this PID file (`src/gobby/cli/daemon.py:309-332`); after the clobber they report "not running" against a live daemon. Exit code 1 also defeats the launchd `KeepAlive.SuccessfulExit=false` design the comment at runner.py:248-249 relies on — launchd respawns and re-clobbers until the winner's health endpoint answers. No test covers `server.serve()` failing (`tests/test_runner_lifecycle.py:69-72,100-103,129-132` always mock `serve` as succeeding).
- **Minimal fix:** (1) Write the PID file only after confirming no live PID owns it (read + `os.kill(pid, 0)`, or `O_CREAT|O_EXCL` with stale-file takeover); (2) observe `server_task` (done-callback or `BaseException` handling) so bind failure triggers the orderly shutdown path including `cleanup_pid_file()`; exit 0 when the loss was "address already in use" and a healthy daemon answers, so launchd stops respawning.
- **Confidence:** high — synthesizer-verified the blind overwrite, the guard, uvicorn's `sys.exit(1)` on `OSError`, and `cleanup_pid_file`'s own-PID-only unlink.

### [BLOCKER] `get_pipeline_executor` fast path discards `project_id` — cross-project pipeline executions

- **Where:** `src/gobby/app_context.py:177-179` (fast path), `:199-203` (lazy path reuses the startup `pipeline_execution_manager` for any `pid`)
- **Failure mode:** The startup executor is pinned to the daemon's CWD project: `runner_init/services.py:242-249` derives `runner.project_id` from `get_project_context(Path.cwd())`, and `runner_init/orchestration.py` builds `LocalPipelineExecutionManager(db, project_id=runner.project_id)` around it. `ServiceContainer.get_pipeline_executor(project_id)` returns that executor for **every** requested project the moment `self.pipeline_executor is not None`. The HTTP route `servers/routes/pipelines.py:276-282` rejects requests without `project_id` and calls `get_pipeline_executor(project_id)` believing the executor is resolved per-project — but the fast path ignores the argument. `create_execution` (`storage/pipelines.py:35-73`) has no project parameter and stamps `self.project_id`, so a pipeline run for project B is recorded under project A; every project-filtered query in that manager (`storage/pipelines.py:169-176, 344-348, 410-414`) then operates on the wrong scope. The lazy path has the same family bug: if `self.pipeline_executor` is None but the startup manager exists, any project's lazily built executor binds to the startup project's manager.
- **Why it matters:** Cross-project data contamination in `pipeline_executions` plus wrong list/approve/stale-fail scoping on a daemon that is machine-global and explicitly multi-project. Triggered any time the daemon was started inside a project directory and a request names a different project.
- **Minimal fix:** Take the fast path only when `project_id in (None, "", self.project_id)`; apply the same guard before reusing `self.pipeline_execution_manager` (create a per-`pid` manager otherwise).
- **Confidence:** high — synthesizer-verified the fast path, the startup pinning (`_LPEM(db=..., project_id=runner.project_id)`), and `create_execution`'s lack of a project parameter.

### [IMPORTANT] Subsystem init and periodic tasks start before the port is bound; `server_task` is never observed at runtime

- **Where:** `src/gobby/runner_lifecycle.py:148-171` (server task, then `_subsystem_init_task`, then `_start_periodic_tasks`, no `server.started` gate); `server_task`'s only other reference is at shutdown (`runner_lifecycle_shutdown.py:506`)
- **Failure mode:** `_init_subsystems` begins side effects (stdio MCP child processes, cron start, agent-run reconciliation against shared Postgres) concurrently with — and independent of — bind resolution. In the double-start race above, the losing instance mutates shared state belonging to the winner until the `SystemExit` lands; how far it gets is unbounded. Separately, because nothing watches `server_task` between creation and shutdown, any later crash of the serve loop leaves the daemon headless: the `while not runner._shutdown_requested` poll spins forever with no HTTP listener.
- **Why it matters:** Cross-instance state mutation against live data; silent loss of the HTTP plane with the process still reporting alive via PID.
- **Minimal fix:** Await `server.started` (or `server_task` completion) before creating `_subsystem_init_task` and periodic tasks; attach a done-callback to `server_task` that requests shutdown if it finishes while `_shutdown_requested` is false.
- **Confidence:** high on ordering; med on how deep a losing instance typically gets.

### [IMPORTANT] Mid-sequence `init_subsystems` failure leaves the daemon permanently degraded and invisible to the startup tracker

- **Where:** entry chain `src/gobby/runner_lifecycle.py:150-154` → `src/gobby/runner_lifecycle_subsystems.py:498-543`; unguarded awaits at `runner_lifecycle_subsystems.py:198` (`message_processor.start()`), `:212` (`lifecycle_manager.start()`), `:274-275` (cron handlers + `cron_scheduler.start()`); failure handler `src/gobby/runner_lifecycle_startup.py:48-60`
- **Failure mode:** Steps are inconsistently guarded — `communications_manager.start()` is wrapped (:203-210) but `message_processor.start()` and `lifecycle_manager.start()` two lines away are not. Any exception there aborts the remainder of the chain (tmux check, agent monitor, cron, code index, pipeline recovery, WebSocket server, automation loop). The only handling is `_log_subsystem_init_result`, which logs once — it does not call `tracker.error()`/`tracker.finish()`, tear down already-started subsystems, retry, or request shutdown. Consequences: `services.startup_ready` (set only at :538) stays False forever, so agent spawning is permanently refused (`src/gobby/agents/readiness.py:12`); `/api/admin/startup-progress` reports `done: false` so `gobby start`'s poll loop burns its full `max_wait`; meanwhile `/api/admin/health` returns unconditional `{"status": "ok"}` (`servers/routes/admin/_health.py:141-143`) so watchdogs consider the daemon healthy. Secondary ordering bug: `startup_ready = True` (:538) is set **before** `_start_system_automation_loop` (:540) and `tracker.finish()` (:542) — an automation-loop failure yields ready=True / done=False, the inverse inconsistency.
- **Why it matters:** Reporting healthy while message processing, session lifecycle, and agent spawning are silently dead is exactly the success-while-contract-violated failure class; one log line is the entire evidence trail.
- **Minimal fix:** Record failures into the tracker from `_log_subsystem_init_result` (`tracker.error(...); tracker.finish()`); wrap the unguarded `start()` calls in the same try/except-with-tracker pattern as their siblings; move `startup_ready = True` after the automation loop start.
- **Confidence:** high.

### [IMPORTANT] WebSocket server startup is fire-and-forget; a WS bind failure is silent for the daemon's lifetime

- **Where:** `src/gobby/runner_lifecycle_subsystems.py:450-454`
- **Failure mode:** `runner._websocket_task = asyncio.create_task(runner.websocket_server.start())` — no done-callback, no task name. `WebSocketServer.start()` awaits `serve(...)` (`servers/websocket/server.py:309-330`), which raises `OSError` if the port is taken. The exception sits unretrieved; because `runner._websocket_task` holds a strong reference, even the GC "Task exception was never retrieved" warning never fires. The tracker records only `schedule("WebSocket server")` — never completed, never errored — and `startup_ready` is set True four lines later. The failure first surfaces at shutdown ("WebSocket startup task failed during shutdown", `runner_lifecycle_shutdown.py:84`). Contrast: the provider-refresh task at :45-50 *does* get a done-callback. (Found independently by two reviewers.)
- **Why it matters:** UI/chat/broadcast clients can't connect for the whole run and neither logs nor the startup tracker say why.
- **Minimal fix:** `add_done_callback` that logs `task.exception()` and records `tracker.error("WebSocket server", ...)`; name the task.
- **Confidence:** high.

### [IMPORTANT] init/shutdown race clobbers `startup_ready`/`shutdown_in_progress` — agent spawning re-enabled during teardown

- **Where:** `src/gobby/runner_lifecycle_subsystems.py:533-539` vs `src/gobby/runner_lifecycle_shutdown.py:469-470` and `:494`
- **Failure mode:** `init_subsystems` runs as a background task while uvicorn serves. `shutdown_daemon_services` sets `startup_ready=False; shutdown_in_progress=True` (:469-470), then awaits a guaranteed multi-second grace window, pending-interaction cleanup, and HTTP drain before cancelling `_subsystem_init_task` at :494. If shutdown is requested while init is still running (easy: `_connect_mcp_servers` alone can take ~10s), the init task resumes inside that window and unconditionally executes `services.startup_ready = True; services.shutdown_in_progress = False` (:538-539) — and nothing re-sets the flags. For the rest of teardown, `spawn_readiness_blocker` (`agents/readiness.py:10-13`) stops blocking, so the dispatcher, spawn path, and cron executor may launch agents into a dying daemon; init even starts *new* subsystems (websocket :452, automation loop :309-317) mid-shutdown. The `shutdown_in_progress = False` write is harmful-only (the default is already False). Existing tests replace `_init_subsystems` wholesale (`tests/test_runner_shutdown.py:593,648`), so the interleaving is untested.
- **Why it matters:** Work dispatched during teardown is killed seconds later, wasting agent runs and leaving half-done task state; the flags' contract is violated.
- **Minimal fix:** Guard the flag write with `if not services.shutdown_in_progress:` and delete the `shutdown_in_progress = False` line; belt-and-braces: cancel `_subsystem_init_task` before the first await in `shutdown_daemon_services`.
- **Confidence:** high.

### [IMPORTANT] A single unexpected exception mid-shutdown aborts all remaining cleanup

- **Where:** `src/gobby/runner_lifecycle_shutdown.py:499-524` (TimeoutError-only catches), `:511-518` (unguarded awaits), `:362-400` (`_stop_started_services`), `:413-422` (`_stop_ui_dev_server_if_needed`, no guard); caller `src/gobby/runner_lifecycle.py:190-194`
- **Failure mode:** Nearly every stop call catches only `TimeoutError`. If `lifecycle_manager.stop()`, awaiting `server_task` (re-raises any stored server crash), `cancel_active_agent_runs_for_shutdown` (DB-heavy), `cron_scheduler.stop()`, `mcp_proxy.disconnect_all()`, or sync `stop_ui_server()` raises anything else, the exception propagates to `run_daemon`'s `except Exception` → `sys.exit(1)`. Everything downstream is skipped: agent cancellation/terminalization, child-process reaping (:529), telemetry flush (:535), `db_executor.shutdown` (:542), `database.close()` (:547), and the active-marker unlink (:553).
- **Why it matters:** One flaky subsystem converts graceful shutdown into an abort: orphaned MCP/agent child processes, agent runs stuck `running` with no `terminal_reason`, unflushed telemetry, leftover `shutdown_intent_active.json`.
- **Minimal fix:** Broaden the per-service catches to `except Exception` (log and continue), or wrap each step in a `_best_effort(coro, name)` helper so the tail of the sequence always runs.
- **Confidence:** high.

### [IMPORTANT] `db_executor.shutdown(wait=True)` blocks the event loop with no timeout

- **Where:** `src/gobby/runner_lifecycle_shutdown.py:542`; `src/gobby/storage/executor.py:103-109` (delegates to `ThreadPoolExecutor.shutdown(wait=True)`)
- **Failure mode:** A synchronous, unbounded join of worker threads executed directly on the event loop. If any queued DB callable is wedged (hung Postgres query, network stall), shutdown hangs *and* the loop is blocked, so the asyncio signal handlers can't run — repeated SIGTERM/Ctrl-C do nothing. The CLI escalates to SIGKILL after 20s (`src/gobby/cli/utils.py`, `max_wait = 20`), abandoning `database.close()` and marker cleanup.
- **Why it matters:** One hung DB thread converts every graceful stop into a SIGKILL; this is the only sync-blocking, untimed call in the sequence.
- **Minimal fix:** `await asyncio.wait_for(asyncio.to_thread(db_executor.shutdown, wait=True), timeout=5.0)` with fallback to `shutdown(wait=False, cancel_futures=True)` on timeout.
- **Confidence:** high.

### [IMPORTANT] `_approval_timeout_task` is started but never cancelled at shutdown

- **Where:** created at `src/gobby/runner_lifecycle_periodic.py:182-190`; missing from the cancel tuple at `src/gobby/runner_lifecycle_shutdown.py:309-324`
- **Failure mode:** Every other periodic task attr (`_metrics_cleanup_task` … `_wiki_watcher_task`, 14 entries) is cancelled; this one is not (repo-wide search: only declaration, creation, and the task-count tuple reference it). The loop sleeps 60s and checks `is_shutdown_requested()` only at the loop top, so a tick landing during/after `db_executor.shutdown`/`database.close()` issues sync DB queries (`get_expired_approval_steps`) against torn-down storage — and may write step-FAILED/execution-CANCELLED rows concurrent with teardown. No test guards this. (Found independently by two reviewers.)
- **Why it matters:** Silent drift from the "all periodic loops cancelled before storage closes" invariant; use-after-close errors and racy pipeline-state writes at shutdown.
- **Minimal fix:** Add `"_approval_timeout_task"` to the cancel tuple; cover it in the shutdown test that exercises periodic-task cancellation.
- **Confidence:** high.

### [IMPORTANT] Restart preservation reads only the first 100 active agent runs

- **Where:** `src/gobby/runner_lifecycle_shutdown.py:44` (`run_storage.list_active()` — default `limit=100`, `src/gobby/storage/agents/_queries.py:164-171`) vs `src/gobby/runner_lifecycle_agents.py:347` (`list_active(limit=1000)` in the cancel path)
- **Failure mode:** On RESTART intent, `_preserved_agent_terminal_pids` builds the preserve set from at most 100 active runs. With >100 active tmux-backed runs, the excess panes' PIDs are absent from the set and `_reap_remaining_child_processes` (:529-532) terminates/kills them — violating the "restart preserves tmux-backed active agent runs" contract. The stop path deliberately uses `limit=1000`, so the two paths disagree on the same population.
- **Why it matters:** Wrong behavior at scale (`max_active_agents` is configurable past 100); the kill is logged as routine reaping.
- **Minimal fix:** `list_active(limit=1000)` (or paginate) at line 44 to match the cancel path.
- **Confidence:** high on the mismatch; the >100-runs precondition is rare with default caps.

### [IMPORTANT] Shutdown-marker writes are non-atomic; a torn marker silently degrades RESTART to STOP

- **Where:** `src/gobby/shutdown_intent.py:95-99` (`marker.write_text(json.dumps(data))`, no temp-file + `os.replace`, no fsync); degrade path `:160-175` (malformed → quarantine → STOP record)
- **Failure mode:** Markers are read cross-process (CLI writes, daemon signal handler consumes, hook subprocesses poll). `write_text` truncates then writes, so a concurrent reader (or crash mid-write) can observe an empty/partial file. The consuming reader treats it as malformed, quarantines it, and resolves to STOP — a restart intent becomes a stop: agents killed and terminalized with `terminal_reason=daemon_stop` instead of preserved.
- **Why it matters:** Intent downgrade is the exact failure class of #15065, reachable through file-level races the fix didn't cover; quarantine also destroys the evidence of what was requested.
- **Minimal fix:** Write to `marker.with_suffix(".tmp")` then `os.replace()` for both marker files in `write_shutdown_intent`.
- **Confidence:** high on non-atomicity; med on real-world hit rate.

### [IMPORTANT] Signal-handler "active marker" fallback is unreachable; restart markers older than 10s silently degrade to STOP

- **Where:** `src/gobby/runner_maintenance.py:784-801` (`_read_signal_shutdown_record`); alias `src/gobby/shutdown_intent.py:76` (`get_active_shutdown_marker_path = get_shutdown_marker_path`)
- **Failure mode:** The fallback to `read_active_shutdown_intent(max_age_seconds=120)` only triggers when `read_shutdown_intent` returned the FileNotFound sentinel — but both functions read the **same file** (`shutdown_intent_active.json`), which was just found missing, so the fallback always returns None outside a microsecond write-between-reads race. Meanwhile the case the 120s window appears designed for — a RESTART marker aged 10–120s when SIGTERM finally lands (slow launchd bootout, delayed force-stop) — takes the other branch: `read_shutdown_intent` (default `max_age_seconds=10.0`, `shutdown_intent.py:128`) consumes it, marks it stale, and `_record_from_marker_data` (:258-275) forces intent to STOP. No rescue runs; if shutdown hadn't already captured RESTART, agents are cancelled.
- **Why it matters:** The code advertises a 120s recovery window that does not exist; effective restart-marker validity is 10s, and the degrade is silent.
- **Minimal fix:** In `_read_signal_shutdown_record`, accept the consumed record when `shutdown_record.stale and shutdown_record.raw` shows `intent=restart` within the 120s window (re-evaluate the already-read `raw` with the longer max age) instead of re-reading the just-deleted file.
- **Confidence:** high on the unreachable branch; med on operational frequency of >10s signal delays.

### [IMPORTANT] Worst-case graceful-shutdown budget far exceeds the CLI's 20s SIGKILL window

- **Where:** `src/gobby/runner_lifecycle_shutdown.py:471` (unconditional 5s grace, `_CRITICAL_STOP_HOOK_GRACE_SECONDS = 5.0` at `:21`), `:506` (uvicorn drain 15s + 5), `:296-340` (14 sequential `_cancel_runner_task` calls × 2s + wiki 2s + sync worker 5s ≈ 35s), per-service 2–5s timeouts in `:356-400`/`:425-449`; supervisor `src/gobby/cli/utils.py` (`max_wait = 20` → SIGKILL)
- **Failure mode:** The teardown is strictly serial and its timeout budget sums to well over a minute in degraded conditions, while `stop_daemon` SIGKILLs after 20s. Any one slow phase (uvicorn lifespan alone can consume the full 20s; the unconditional 5s grace eats 25% of budget up front) means the tail — agent terminalization, child reaping, DB close, marker unlink — is killed mid-flight.
- **Why it matters:** Graceful shutdown degrades to SIGKILL exactly in the degraded situations it was built for, and the cleanup skipped is the cleanup that matters.
- **Minimal fix:** Cancel independent periodic tasks concurrently (`asyncio.gather(..., return_exceptions=True)`) and cap the whole sequence with an outer deadline below 20s; skip/shorten the 5s grace on RESTART intent.
- **Confidence:** med — exact wall-clock depends on which phases degrade together, but the budget arithmetic and the 20s kill are pinned.

### [IMPORTANT] RESTART-vs-STOP precedence is enforced only on the signal path; HTTP writes can still clobber intent

- **Where:** `src/gobby/runner.py:185-189` (`request_shutdown` overwrites `_shutdown_intent` unconditionally), `src/gobby/servers/routes/admin/_lifecycle.py:192-193` (raw attribute fallback), vs the guard living only in the signal closure (`src/gobby/runner_maintenance.py:782, 805-829`); capture-once at `runner_lifecycle_shutdown.py:466`
- **Failure mode:** The #15065 protection (first recorded intent wins) exists only inside `setup_signal_handlers`' closure. If a restart is initiated and a concurrent `POST /api/admin/shutdown` (writes STOP) lands before `shutdown_daemon_services` captures the intent at :466 (up to 0.5s poll latency plus scheduling), RESTART is overwritten with STOP and active agents are cancelled with `terminal_reason=daemon_stop` — the same downgrade class #15065 fixed, through a different door.
- **Why it matters:** The precedence contract is a property of one of three writers, not of the intent state itself.
- **Minimal fix:** Move the precedence check into `GobbyRunner.request_shutdown` (refuse RESTART→STOP downgrade once `_shutdown_requested` is set) and route the `_lifecycle.py` fallback through it.
- **Confidence:** med — sub-second window needing concurrent stop+restart, but nothing prevents it.

### [IMPORTANT] Pipeline recovery and wiki cron are silently pinned to the startup-CWD project (or skipped entirely)

- **Where:** `src/gobby/runner_lifecycle_subsystems.py:376-379` (guard), `:390` (`resume_interrupted_pipelines(project_id=runner.project_id)`), `:284-285` (wiki cron: `if not runner.project_id ... return`)
- **Failure mode:** `_recover_pipelines` returns silently unless the startup executor/manager exist — and those are only created when the daemon started inside a project (`runner.project_id` from `Path.cwd()`). When skipped, no tracker entry is emitted. Even when it runs, interrupted/stale-execution recovery covers only that one project: executions belonging to every other project stay `RUNNING`/`INTERRUPTED` forever and their completion subscribers are never woken. `_register_wiki_cron_handlers` has the same shape — a daemon started outside a project registers no wiki cron jobs, silently.
- **Why it matters:** A machine-global daemon performs restart recovery for at most one arbitrary project chosen by start-time CWD; stale executions accumulate; agents waiting on completion notifications hang.
- **Minimal fix:** Iterate recovery over projects with pipeline executions (or run stale-fail with a project-unscoped manager); emit `tracker.error("Pipeline recovery", "skipped: no startup executor")` when the guard fires.
- **Confidence:** med-high — pinning verified at the cited lines; impact contingent on multi-project use, which the routes explicitly support.

### [IMPORTANT] Sync DB and sync work on the event loop across init, maintenance, and dispatch

- **Where (init, while HTTP is serving):** `src/gobby/runner_lifecycle_subsystems.py:148` (`cleanup_old_metrics()`), `:173` (`list_memories(limit=10000)` — sync, `storage/memories.py:422`), `:395` (`fail_stale_running_executions`), `:424-447` (subscriber reads/writes)
- **Where (maintenance loops):** `src/gobby/runner_maintenance.py:526-530` (metric snapshot save/prune every 60s), `:482-493` (approval expiry every 60s), `:264-281` (zombie UPDATE at startup + every 6h), `:196` (span DELETE), `:151`, `:171`, `:337` (sync `update_title` via `storage/sessions/_field_update.py:173-211`)
- **Where (agents/dispatch):** `src/gobby/runner_lifecycle_agents.py:65, :131, :168-175, :192, :347` (sync storage + mutex manager in async context); every automation tick via `src/gobby/dispatch/dispatcher.py:158-186` (`sweep_orphan_no_run_dispatch_mutexes`, `sweep_stale_claims`, `list_automation_candidates`, `count_active_agents` per candidate)
- **Failure mode:** All of these run psycopg I/O (or 10k-row hydration) directly on the daemon's single event loop. Retention DELETEs are unbounded — after downtime the backlog can take seconds, stalling HTTP, WebSocket, MCP proxy, and hook handling. The codebase already provides the right facilities — `_run_db` (`runner_maintenance.py:55-63`), `DatabaseExecutor`/`services.run_db` (`app_context.py:126-130`), `SystemAutomationLoop._run_db_call` (`system_automation.py:565-568`) — and some call sites use them; the rest bypass them. A side effect: a task cancelled mid-sync-call can't observe cancellation, so `_cancel_runner_task`'s 2s `wait_for` times out and shutdown proceeds to close storage with work still in flight.
- **Why it matters:** Periodic latency spikes for every daemon client, worst at startup and after long downtime — exactly when backlogs are largest; degraded shutdown.
- **Minimal fix:** Route every listed call through the executor (`_run_db`/`services.run_db`/`to_thread`); batch/LIMIT the retention DELETEs.
- **Confidence:** high (sync calls verified; stall magnitude data-dependent). (Sites contributed by three reviewers; merged as one failure class.)

### [IMPORTANT] Pre-loop exceptions kill periodic jobs silently — dead job until restart, with no log ever

- **Where:** `src/gobby/runner_lifecycle_periodic.py:93-204` (all 14 `asyncio.create_task` calls, no `add_done_callback`); vulnerable pre-loop code at `src/gobby/runner_maintenance.py:190-192`, `:371-375`, `:520-523`, `:557-561`
- **Failure mode:** Each loop's `try/except Exception` wraps only the iteration body; constructor/import failures before the `while` terminate the coroutine with the exception retained on the Task. Because the Task is pinned to a runner attribute for the daemon's lifetime, it is never GC'd, so the "Task exception was never retrieved" handler never fires — the job is dead with zero log lines until restart. The codebase has the right pattern elsewhere (`runner_broadcasting.py:149`, `runner_lifecycle.py:154`) but not here.
- **Why it matters:** Span/metrics/comms retention, approval expiry, and isolation reaping can silently stop, producing unbounded growth and stuck pipelines with no diagnostic.
- **Minimal fix:** Attach one shared done-callback in `start_periodic_tasks` that logs `task.exception()` (ignoring `CancelledError`) for every created task.
- **Confidence:** high.

### [IMPORTANT] Approval-timeout expiry is non-idempotent: a partial failure strands the execution forever

- **Where:** `src/gobby/runner_maintenance.py:484-503`; query filter at `src/gobby/storage/pipelines.py:786` (`WHERE se.status = waiting_approval`)
- **Failure mode:** The loop sets the step to FAILED (:485-489), then the execution to CANCELLED (:490-493). If the second call raises (caught at :498-503), the step is no longer `waiting_approval`, so `get_expired_approval_steps` never returns it again — the parent execution stays non-terminal permanently; no retry path exists.
- **Why it matters:** Pipeline executions stuck "running" forever; the loop's documented contract (docstring :472-475) is violated after one error line.
- **Minimal fix:** Reverse the order (cancel execution first, then fail the step), or do both updates in one transaction; alternatively sweep for executions whose steps are all terminal.
- **Confidence:** med-high — no other reconciler cancels executions with failed steps (searched).

### [IMPORTANT] 24h sleep-first loops never run on daemons with <24h uptime — unbounded growth

- **Where:** `src/gobby/runner_maintenance.py:148-152` (`metrics_cleanup_loop`), `:168-171` (`metrics_archive_loop`), `:230-233` (`memory_reconcile_loop`), `:377-381` (`cleanup_comms_messages_loop`) — all `await asyncio.sleep(24h)` *before* the first unit of work
- **Failure mode:** A local-first daemon on a developer laptop restarts (or the machine sleeps, stalling the timer) well inside 24h. Each restart resets the timer, so these four jobs can go weeks without one execution: tool-metrics rows, metrics events, comms messages, and Qdrant/FalkorDB orphans accumulate without bound. The file itself shows the correct pattern — `span_cleanup_loop` (:193-196) and `cleanup_zombie_messages_loop` (:279-283) run work-first.
- **Why it matters:** Unbounded table growth on exactly the deployment profile Gobby targets; when the loop finally fires, the backlog DELETE is huge and runs sync on the event loop (see sync-DB finding).
- **Minimal fix:** Run one cleanup shortly after startup (small randomized delay), then sleep the interval — or persist a last-run timestamp and sleep the remainder.
- **Confidence:** high on mechanism; med on real-world frequency.

### [IMPORTANT] Interval tick bypasses per-project dispatch dedupe — same-project work can overlap

- **Where:** `src/gobby/system_automation.py:510-528` (`_dispatch_projects` calls `dispatch_project_once` directly), `:454-478` (scheduled path), `:390-413` (dedupe via `_project_tasks` covers only the scheduled path)
- **Failure mode:** The interval loop dispatches every enabled project without consulting `_project_tasks`, so a triggered dispatch for the same project can overlap its recovery, candidate scanning, and tick bookkeeping. Heartbeat admission is serialized separately, but the project-level scheduling contract is still bypassed.
- **Why it matters:** Duplicate same-project work wastes a tick, produces inconsistent summaries, and prevents triggered wakes from coalescing behind the active interval run.
- **Minimal fix:** Route interval-tick dispatches through `_schedule_project_dispatch_on_loop` (so `_project_tasks` serializes all dispatch per project), or wrap `dispatch_project_once` in a per-project `asyncio.Lock`.
- **Confidence:** high.

### [IMPORTANT] The "global agent-slot cap" is actually per-project

- **Where:** `src/gobby/dispatch/dispatcher.py:169-177` and `:729-752` (`count_active_agents` filters `parent_s.project_id = %s` when `project_id` is given); every automation path passes `project_id` (`src/gobby/system_automation.py:310-317`)
- **Failure mode:** CLAUDE.md's Dispatch Architecture section states "a global agent-slot cap (`max_active_agents`, default 10)". Because `run_heartbeat` is always invoked per-project, the count is project-scoped: N enabled projects can run up to 10×N agents. The global-count branch is unreachable from the automation loop.
- **Why it matters:** Resource limiting silently scales with project count; an operator relying on the documented cap of 10 gets 30 agents with 3 enabled projects — compounding the race above.
- **Minimal fix:** Decide intent: check the global count (`count_active_agents(db, project_id=None)`) alongside or instead of the scoped count, or fix CLAUDE.md/dispatch docs to say per-project.
- **Confidence:** high that behavior diverges from the documented contract; med on which side is intended.

### [IMPORTANT] Restart reconciliation never checks PID liveness for non-tmux runs — dead agents hold slots and mutexes up to 30 minutes

- **Where:** `src/gobby/runner_lifecycle_agents.py:98-101` (non-tmux runs get only a mutex refresh); `src/gobby/agents/agent_health.py:159-178` (death detection `os.kill(run.pid, 0)` runs only inside the tmux branch); `src/gobby/storage/agents/_cleanup.py:28` (30-min inactivity timeout is the only reaper)
- **Failure mode:** A non-tmux agent whose process died while the daemon was down (or during restart — non-tmux children share the daemon's lifetime) is reconciled as healthy: `_refresh_active_run_dispatch_mutex` re-acquires a 600s dispatcher lease for its task, and the lifecycle monitor keeps renewing it because the run is still in `list_active`. No monitor path PID-checks non-tmux runs (verified: liveness checks exist only at `agent_health.py:163` and `agents/tmux/pane_monitor.py:167`). The run stays `running` until the 30-min inactivity timeout.
- **Why it matters:** Per dead run, for up to ~30 minutes: a slot consumed against `max_active_agents`, the task mutex-locked against re-dispatch, the task stranded in progress. Several dead runs after a crash can stall dispatch entirely.
- **Minimal fix:** In `_reconcile_agent_runs_after_restart`, `os.kill(run.pid, 0)`-check `non_tmux_runs` (when `run.pid` is set) and route dead ones through the same cleanup handler as `_cleanup_missing_tmux_agent_run` instead of refreshing their mutex.
- **Confidence:** high.

### [IMPORTANT] Pagination loops never paginate — rows beyond the first page are silently skipped

- **Where:** `src/gobby/runner_lifecycle_agents.py:62-87` (`_recover_agent_runs_after_restart`), `:191-204` (`_list_active_agent_runs_once`), `:245-249` (`_replay_daemon_restart_agent_cancellations`); storage at `src/gobby/storage/agents/_queries.py:164` (`list_active(limit, offset=0, ...)`) and `:106` (`list_by_status` has **no** offset parameter)
- **Failure mode:** Each loop re-calls `list_active(limit=N)` / `list_by_status("cancelled", limit=N)` without an offset; the second iteration returns the identical first page (stable ORDER BY), `new_in_batch` is 0, the loop breaks. Rows beyond the first page are never processed. The correct cursor pattern exists at `src/gobby/agents/lifecycle_monitor.py:357-402`.
- **Why it matters:** Active runs >500 is unrealistic today, but the loop shape promises coverage it can't deliver, and `list_by_status("cancelled", limit=500)` reads an unbounded growing set where >500 rows is plausible.
- **Minimal fix:** Thread `offset=len(seen_ids)` through (add an offset to `list_by_status`), or delete the while-loops and document the single-page read.
- **Confidence:** high on behavior; med on practical impact.

### [IMPORTANT] Project dispatches can be scheduled after `stop()` — tasks leak into daemon teardown

- **Where:** `src/gobby/system_automation.py:162-190` (`schedule_project_dispatch` has no `_running` check), `:390-413` (`_schedule_project_dispatch_on_loop` likewise), `:140-155` (`stop()` clears/cancels only what exists at call time)
- **Failure mode:** A trigger queued via `loop.call_soon_threadsafe` just before/during `stop()` executes after `stop()` completes, creates a fresh task in the now-dead `_project_tasks` map, and runs `dispatch_project_once` → `recover_safe_build_claims` (:307) and config reads against a database being shut down. Agent spawning is blocked by the readiness guard, and exceptions are swallowed (:471-478), so the symptom is stray DB writes racing `db_executor.shutdown()` and an orphaned pending task at interpreter exit.
- **Why it matters:** Shutdown-race writes against a closing pool are nondeterministic; "Task was destroyed but it is pending" noise masks real teardown bugs.
- **Minimal fix:** Guard both scheduling entry points with `if not self._running: return`.
- **Confidence:** high on mechanism; low-med on impact.

### [IMPORTANT] One slow WebSocket consumer blocks all broadcasts (head-of-line blocking)

- **Where:** `src/gobby/servers/websocket/broadcast.py:91-124` (sequential loop at :107, `await websocket.send(message_str)` at :112) — the wiring target every `runner_broadcasting.py` callback funnels into (`broadcast_agent_event` → `runner_broadcasting.py:203-214`; `broadcast_terminal_output` at :100-103)
- **Failure mode:** `broadcast()` awaits each client's `send()` sequentially with no per-send timeout. A client whose TCP buffer is full (slow or partitioned but not yet `ConnectionClosed`) blocks the await until its buffer drains or the OS TCP timeout fires; every later client — and the whole broadcast — stalls behind it. High-frequency terminal output makes this acute: one stalled browser tab freezes live output for all others. Closed sockets are handled (:113-115); *stalled* sockets are not.
- **Why it matters:** A single misbehaving UI client degrades real-time delivery for every connected client.
- **Minimal fix:** Bound each send with `asyncio.wait_for(...)`, or fan out with `asyncio.gather(..., return_exceptions=True)`; drop/close clients that time out.
- **Confidence:** high.

### [IMPORTANT] Blocking + under-guarded file I/O on the event loop in gwiki health normalization

- **Where:** `src/gobby/gwiki_gateway.py:383-405` (`_normalize_health_report_heading`), reached from async `health()` at `:189-192`
- **Failure mode:** (1) `report_path.read_text()` and `write_text(...)` are synchronous filesystem calls on the event loop. (2) The read is guarded only by `except OSError`; a non-UTF-8 report raises `UnicodeDecodeError` (a `ValueError`), which escapes and turns a successful `gwiki health` into a 500. (3) The final `write_text` is outside any try block — a read-only filesystem or full disk raises and crashes the endpoint.
- **Why it matters:** A cosmetic heading-normalization step can crash `/api/wiki/health` and stall the loop despite the underlying command having succeeded — failure reported while the contract was satisfiable.
- **Minimal fix:** `await asyncio.to_thread(...)` the I/O and broaden the guard to `except (OSError, ValueError): return` around both read and write; treat normalization as best-effort.
- **Confidence:** high.

### [IMPORTANT] `GOBBY_HOME=""` silently redirects gobby home to the current directory

- **Where:** `src/gobby/paths.py:74-76` — `Path(os.environ.get("GOBBY_HOME", Path.home() / ".gobby"))`
- **Failure mode:** `os.environ.get` returns the default only when the key is *absent*. `GOBBY_HOME` exported but empty (`env GOBBY_HOME= ...`, blank CI vars, `export GOBBY_HOME=$UNSET`) yields `Path("")` → `PosixPath('.')`. Every dependent path (`get_global_workflows_dir`, `get_global_rules_dir`, `get_global_pipelines_dir`, `get_global_agents_dir`, `get_global_variables_dir`, :79-101) becomes `./workflows`, `./rules`, etc., rooted at the process CWD.
- **Why it matters:** Global rules/workflows/agents read from and written under whatever directory the daemon launched in — wrong config loaded, files scattered into project trees, CLI/daemon divergence when their CWDs differ.
- **Minimal fix:** `value = os.environ.get("GOBBY_HOME"); return Path(value) if value else Path.home() / ".gobby"`.
- **Confidence:** high.

### [IMPORTANT] Fire-and-forget broadcast tasks hold no strong reference (inconsistent with the codebase's own pattern)

- **Where:** `src/gobby/runner_broadcasting.py:148, 162, 176, 184, 199, 203` — each `task = asyncio.create_task(...)` + `add_done_callback(_log_broadcast_exception)`; no module-level task set
- **Failure mode:** The event loop keeps only a weak reference to tasks; `add_done_callback` does not add a strong one. Tasks scheduled-but-not-yet-stepped (or suspended without an external referent) can be garbage-collected mid-flight, silently dropping the broadcast. `src/gobby/servers/app_factory.py` (`_broadcast_session_change`) deliberately anchors tasks in a set + discard-on-done — the safe pattern exists and isn't applied here.
- **Why it matters:** Intermittent, hard-to-reproduce loss of agent/tmux lifecycle broadcasts under GC pressure.
- **Minimal fix:** Module-level `set[asyncio.Task]`, `.add(task)` on creation, discard in the done callback (mirror `app_factory`).
- **Confidence:** low-med — the documented hazard is real; in practice the first scheduling step usually keeps these alive.

### NIT sweep re-audit (2026-07-11)

The NIT entries below are retained as review-at-commit evidence, not as a live backlog. A current-code
re-audit found that related daemon-core work had already fixed or separately captured the other
findings; creating another leaf for them would duplicate focused work. Three uncaptured residuals
remained and were split under coordination epic #17824, then resolved in the daemon-core worktree:

- #17914 removed the last dead, consuming `read_shutdown_source()` compatibility helper. The older
  `write_stop_intent()` and `write_restart_intent()` examples were already absent.
- #17915 removed the unreachable daemon-restart cancellation replay, its dead tmux cleanup path and
  compatibility exports, and the unused `daemon_restart` terminal-reason variant. Planned restarts
  preserve active runs; daemon stops use `daemon_stop`.
- #17916 isolated `_dispatch_projects()` failures with `return_exceptions=True`, project-specific
  traceback logging, successful sibling results, and explicit cancellation propagation.

Use those focused tasks and current symbols for implementation history; do not mint new work directly
from the historical line references below without another current-code audit.

### [NIT] Signal-handler installation hard-crashes on Windows while the adjacent fd-limit helper deliberately no-ops there

- **Where:** `src/gobby/runner_maintenance.py:834` (`loop.add_signal_handler`, no fallback); contrast `src/gobby/runner.py:229-232` (`resource` ImportError handled with a "(Windows?)" debug message)
- **Note:** On Windows event loops `add_signal_handler` raises `NotImplementedError` → "Fatal error" → exit 1 before the PID file or server exist. Either Windows is out of scope (then the fd-limit hedge is dead weight) or this is a boot blocker. Minimal fix: `try/except NotImplementedError` falling back to `signal.signal`.

### [NIT] `run_daemon`'s blanket `except Exception` also wraps the graceful-shutdown path

- **Where:** `src/gobby/runner_lifecycle.py:176-194` (shutdown call inside the same try as startup)
- **Note:** An exception escaping `shutdown_daemon_services` is logged as startup-style "Fatal error" and aborts remaining teardown, exiting 1 even for an otherwise clean stop — launchd sees a crash and respawns. Give the shutdown call its own try/except that continues best-effort with `cleanup_pid_file()` and exits 0 when the stop was user-requested.

### [NIT] Catch-all handlers in every maintenance loop drop the traceback (no `exc_info`)

- **Where:** `src/gobby/runner_maintenance.py:108, 157, 179, 202, 221, 246, 283, 292, 354, 363, 390, 455, 464, 506, 536, 632`; also `src/gobby/hooks/inbox.py:203, 215`
- **Note:** `logger.error(f"Error in X loop: {e}")` with no stack; the per-item handlers in the same file use `exc_info=True` (:499-503, :600-603, :618-621). When a retention loop degrades, the one-line message is the entire forensic record. Add `exc_info=True`.

### [NIT] Production-dead sync cleanup trio; unit test exercises the dead variant

- **Where:** `src/gobby/runner_maintenance.py:635-652` (`_cleanup_missing_isolation_records`), `:704-718`, `:721-735`
- **Note:** Repo-wide search shows the only production caller of the missing-record sweep is the async variant (:623); the sync trio's sole caller is `tests/test_runner_maintenance_isolation.py:61`. Delete the sync trio and port the test to the async twin before they drift.

### [NIT] Startup thundering herd — only bin-freshness has initial delay/jitter

- **Where:** immediate-work sites `src/gobby/runner_maintenance.py:281, 196, 526-530, 351-352, 450-451`, `src/gobby/hooks/inbox.py:196-200`; contrast `:97-101` (bin freshness `initial_delay_seconds` + jitter)
- **Note:** All fire in the same tick during startup, serializing sync work on the loop exactly while the daemon is trying to become ready. Stagger with small jittered initial delays.

### [NIT] Planned-restart guards almost never see the marker they key on

- **Where:** consumption at `src/gobby/runner_maintenance.py:786` (first SIGTERM, ms after the CLI writes it); final unlink at `runner_lifecycle_shutdown.py:553`; readers `src/gobby/hooks/health_gate.py:63-69`, `src/gobby/utils/daemon_client.py:118-169`
- **Note:** The active marker is consumed at signal receipt (CLI path) or unlinked at shutdown completion (HTTP path) — gone for essentially the whole daemon-down window the "planned restart" guards cover, so hook subprocesses log "Daemon not running" warnings on every planned restart. Impact is logging/attribution only (`health_gate` allows on both branches), but recurring false warnings train operators to ignore real outages.

### [NIT] Dead marker API: `read_shutdown_source()`, `write_stop_intent()`, `write_restart_intent()` — and the first is a footgun

- **Where:** `src/gobby/runner_maintenance.py:769-773`; `src/gobby/shutdown_intent.py:115-122`
- **Note:** Zero callers repo-wide (gcode grep/usages searches). `read_shutdown_source()` is named like a status read but calls `read_shutdown_intent` with default `consume=True` — any future caller wanting attribution would destroy a pending restart intent. Delete all three or reimplement on `read_shutdown_source_record`.

### [NIT] `_cancel_runner_task` abandons tasks silently and never retrieves prior failures

- **Where:** `src/gobby/runner_lifecycle_shutdown.py:286-293`
- **Note:** On cancel timeout it `pass`es with no log — the task may still run while storage closes underneath it. Already-done tasks are skipped, leaving their exceptions unretrieved. Log timeouts with the attr name; call `task.exception()` on done tasks.

### [NIT] Signal closure freezes the first record and records it before the callback succeeds

- **Where:** `src/gobby/runner_maintenance.py:809-829` (`recorded_shutdown = shutdown_record` at :816, before the callback try at :818-821)
- **Note:** (a) Fix 760ea9fa5 originally blocked only RESTART→STOP downgrade; the current code freezes the first record entirely, so a later `gobby restart` marker after an external STOP signal is neither consumed nor honored — an undocumented contract change. (b) If the intent callback raised on the first signal, the intent is never retried (practically unreachable today — the callback is a `setattr`). Set `recorded_shutdown` after the callback succeeds; document freeze-first semantics.

### [NIT] Defensive `getattr` on attributes that are statically declared hides wiring drift

- **Where:** `src/gobby/runner_lifecycle_subsystems.py:41` (`provider_model_catalog` — declared at `app_context.py:104`), `:286` (`cron_scheduler.executor` — always set, `scheduler/scheduler.py:43`), `:313` (`system_automation_loop` — declared, `runner.py:160`)
- **Note:** A rename/typo silently disables the subsystem instead of raising — the class of bug mypy would catch with direct attribute access. Keep `getattr` only where genuinely optional.

### [NIT] `ServiceContainer` caches never invalidate; `get_git_manager` swallows errors to `None`

- **Where:** `src/gobby/app_context.py:149-165` (cache + `except (ValueError, OSError): return None`), `:241` (per-project executor cached forever)
- **Note:** A project whose `repo_path` changes keeps serving a `WorktreeGitManager` built from the old path for the daemon's lifetime; construction failures are indistinguishable from "project not found". Key the cache on `(project_id, repo_path)`; log before returning None.

### [NIT] Global `_current_container` is set-only; ~25 `Any | None` fields defeat type checking on the wiring surface

- **Where:** `src/gobby/app_context.py:253-264` (no clear/reset anywhere — repo-wide search), `:44-122` (`Any | None` for pipeline_executor, workflow_loader, cron_scheduler, etc.)
- **Note:** Late-shutdown consumers read a container whose DB is closing; and because the heavily-traversed fields are `Any`, a wiring transposition in `runner_init/servers.py:33-76` (40+ keywords) type-checks cleanly. Convert the commented type hints into `TYPE_CHECKING` forward refs; optionally add `clear_app_context()` at end of shutdown.

### [NIT] Telemetry initialized from the pre-override config copy

- **Where:** `src/gobby/runner_init/storage.py:40` (first `load_config` + `init_telemetry`), `:89-95` (reload with `secret_resolver` + `config_store` overrides)
- **Note:** Telemetry holds settings from load #1 while everything wired afterward sees load #2 — DB-store overrides of telemetry fields never apply. Re-apply `init_telemetry` after the second load. Confidence med: requires the store to override telemetry keys.

### [NIT] `_replay_daemon_restart_agent_cancellations` is dead code with no producer

- **Where:** `src/gobby/runner_lifecycle_agents.py:234-303`; transitively dead `_cleanup_lingering_daemon_restart_tmux_session` (:306-336) and `_RunStorageWithTmuxCleanup` (:16-17); stale re-export at `src/gobby/runner_lifecycle.py:19,55`
- **Note:** Zero call sites, and nothing writes `terminal_reason="daemon_restart"` anywhere (only the `Literal` at `storage/agents/_constants.py:10` and this function's reads) — `daemon_stop` resume goes through `try_resume_daemon_stop_run` in the dispatcher instead. 100 lines of unreachable recovery logic (with its own latent pagination bug) that reads as load-bearing. Delete it.

### [NIT] `_dispatch_projects` gathers without `return_exceptions` — one project's failure discards the tick and detaches siblings

- **Where:** `src/gobby/system_automation.py:518-527`
- **Note:** One raising `dispatch_project_once` propagates immediately: all per-project summaries are lost and the sibling coroutines keep running detached after the tick lock releases — overlapping the next tick (compounds the cap race). Use `return_exceptions=True` and fold errors into the tick summary.

### [NIT] `reconciled` counter conflates runs with sub-events

- **Where:** `src/gobby/runner_lifecycle_agents.py:96-149` (one run can increment three times); logged "Reconciled %d active agent run(s)" at `runner_lifecycle_subsystems.py:243-247`
- **Note:** Startup log overstates reconciled-run count (e.g. "9" for 3 runs) — a misleading ops signal during exactly the restarts this code exists for. Count distinct run IDs or rename to "action(s)".

### [NIT] gwiki subprocess stderr is forwarded to HTTP clients

- **Where:** `src/gobby/gwiki_gateway.py:45-57` (`GwikiCommandError.to_envelope` embeds raw stderr) → `src/gobby/servers/routes/wiki.py:287-293` (502 with the envelope as detail)
- **Note:** Raw stderr (absolute paths, internal config, stack traces) returned verbatim. Low impact on loopback, worth sanitizing if the daemon is ever bound beyond localhost: truncate/redact in `to_envelope`, log full text server-side.

### [NIT] Synchronous tempfile writes during wiki upload staging

- **Where:** `src/gobby/servers/routes/wiki.py:404-420` (`_stage_upload`, write at :412, 64 KiB chunks)
- **Note:** `staged.write(chunk)` blocks the loop between async reads; chunking bounds each stall. Wrap in `asyncio.to_thread` or use an async file API.

## Systemic patterns

1. **Failure semantics are accidental, not designed.** Boot: per-step try/except in `init_subsystems` is inconsistent (communications guarded; message processor, lifecycle manager, cron not), so which failure aborts vs. degrades is a coincidence of authorship. Shutdown: the dominant idiom is `except TimeoutError` (14 sites), which protects against slow subsystems but lets any *failing* one abort the rest of teardown. Both ends are one unexpected exception from `sys.exit(1)` with cleanup skipped.

2. **Fire-and-forget `asyncio.create_task` without failure observation.** `server_task` (`runner_lifecycle.py:148`), `_websocket_task` (`runner_lifecycle_subsystems.py:452`), `_vector_rebuild_task` (:183-186), all 14 periodic loops, and the broadcast helpers each handle (or mishandle) task-failure visibility per-site. Done-callbacks exist in three places and are absent in a dozen. A single `spawn_observed(coro, name, tracker)` helper would close the class.

3. **Sync DB on the event loop is opt-in.** `_run_db`, `DatabaseExecutor`/`services.run_db`, and `_run_db_call` all exist; roughly half the call sites in this area bypass them. The executor should be the only way lifecycle/maintenance/dispatch code touches storage.

4. **Startup-CWD project pinning leaks into machine-global surfaces.** `runner.project_id` comes from `Path.cwd()` at daemon start and silently scopes the pipeline executor fast path (Blocker), pipeline restart recovery, and wiki cron registration. Any future consumer of `ServiceContainer.project_id` inherits the trap.

5. **Subprocess calls without an explicit repo path in a multi-project daemon.** The isolation reaper (Blocker) repeats the exact bug already documented for the workflows surface (`docs/reviews/workflows-rules.md`, `docs/reviews/workflows-engine.md`): any `subprocess.run(["git", ...])` in daemon code without `-C <repo>` operates on a random repo. A `run_git(repo_path, ...)` helper making the path mandatory would close the class.

6. **Process-level invariants enforced by best-effort probes instead of atomic primitives.** PID file written by blind overwrite; double-start prevented only by an HTTP health probe whose blind window spans the slowest part of startup; shutdown-intent markers written non-atomically and consumed/aliased by two readers with different staleness windows (10s vs 120s) behind two names for the same file. The unreachable fallback, the guard starvation, and the dead helpers all grew from that split ownership.

7. **Serialization is path-dependent, not resource-dependent.** Per-project dispatch is serialized only when it arrives via `schedule_project_dispatch`; the interval tick and the no-loop fallback bypass it. Locks/dedupe should live on the resource (project, task), not the entry point.

8. **Hand-maintained parallel lists drift.** Periodic tasks are created in one file and cancelled via a hand-copied attr tuple in another; `_approval_timeout_task` already fell through the gap, and the preserve-vs-cancel paths disagree on `list_active` limits (100 vs 1000). Registering created tasks in a single collection that shutdown iterates removes the class.
