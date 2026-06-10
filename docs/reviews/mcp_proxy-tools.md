# Review: mcp_proxy/tools

- **Scope:** `src/gobby/mcp_proxy/tools/` — all top-level modules plus the
  `plans/`, `sessions/`, `skills/`, `spawn_agent/`, `tasks/`, `workflows/`, and
  `worktrees/` subpackages (~33,000 lines). Split boundary: `mcp_proxy` core
  (server/manager/transports/instructions) is covered by the separate
  "mcp_proxy core" leaf.
- **Reviewer:** Claude (Fable 5) — 7 parallel chunk reviewers + synthesizer;
  all Blockers and the doc-drift systemic independently re-verified by the
  synthesizer against source.
- **Commit / branch:** `58c79446a` / `0.5.0`. Working tree carried unrelated
  in-flight edits to `sessions/_handoff.py`, `sessions/_terminal.py`,
  `wait_tools.py` at review time; findings in those files should be re-checked
  against the committed state before fixing.
- **Summary:** 5 Blocker · 64 Important · 44 Nit — the package is feature-rich
  but systemically weak on concurrency (check-then-act everywhere), event-loop
  hygiene (sync git/DB on the loop), and silent-failure discipline
  (success-shaped responses when nothing happened).

## Findings

### Agents & spawn (`agents.py`, `agent_*.py`, `apply_persona.py`, `spawn_agent/`)

### [IMPORTANT] Worktree/clone leaked on every post-prepare spawn failure
- **Where:** `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:602-626, 783-787, 962-975`
- **Failure mode:** `prepare_environment` (line 604) creates a real worktree/clone plus storage record. Cleanup only runs if `prepare_environment` itself raises (607-610). Later failures — provider MCP config error (613-616), planner code-index preflight (625-626), `execute_spawn` failure, tmux live-pane verification flipping success to False (783-787) — mark the run failed and return an error payload that omits `worktree_id`/`clone_id`. The run row never carries the isolation IDs (`storage/agents/_lifecycle.py:71-105`), and `_persist_spawn_runtime` only runs on success (789-815).
- **Why it matters:** Each failed spawn leaks a worktree/branch or full clone with a dangling record nothing references; dispatcher retry loops accumulate one orphan per attempt. Tests cover only the prepare-failure cleanup.
- **Minimal fix:** On every failure return after `prepare_environment` succeeds, call `handler.cleanup_environment` (skip reused isolation), and/or persist isolation IDs to the run row before `execute_spawn`.
- **Confidence:** high

### [IMPORTANT] `kill_agent(status="error")` never notifies completion subscribers and skips task-claim recovery
- **Where:** `src/gobby/mcp_proxy/tools/agent_cancellation.py:102-111` (called from `agents.py:654-663`)
- **Failure mode:** The error branch of `terminalize_killed_agent_run` calls `run_storage.fail(...)` and returns — no `completion_registry.notify`, no task-claim recovery, unlike the success and cancelled paths (70-89). Parents auto-subscribed via `subscribe_agent_completion` never get the event; the claim is only released by the background monitor sweep minutes later. `fail()` also nulls `terminal_reason`.
- **Why it matters:** An agent ending itself with `status="error"` (documented at `agents.py:561-563`) stalls orchestration built on completion subscriptions.
- **Minimal fix:** In the error branch, run claim recovery (outcome="failed") and `await completion_registry.notify(...)`, mirroring the cancelled path.
- **Confidence:** high

### [IMPORTANT] `send_message` reports failure (inviting duplicate sends) when only the WS broadcast fails
- **Where:** `src/gobby/mcp_proxy/tools/agent_messaging.py:238-242`
- **Failure mode:** By the time the broadcast loop runs, `mailbox.send` has already persisted and queued the message. A failing optional `broadcast_fn` (UI-only) sets `payload["success"] = False`; line 239 also clobbers the mailbox's own `failed_broadcasts` list.
- **Why it matters:** Callers retry on `success: false` → duplicate messages injected into the recipient's context; real mailbox broadcast failures are hidden.
- **Minimal fix:** Keep `success` = `send_result.success`; report WS failures under a separate key; merge, don't overwrite, `failed_broadcasts`.
- **Confidence:** high

### [IMPORTANT] TOCTOU race in per-task spawn dedup — concurrent spawns both pass the gate
- **Where:** `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:497-506` (gate) vs run-row insert at `:762`
- **Failure mode:** `_active_task_spawn_blocker` is a plain SELECT; the `agent_runs` row that would block a second caller is inserted only inside `execute_spawn`, after isolation prep and preflight — seconds-to-minutes window. No unique partial index on active runs per task; MCP spawn path takes no `task_dispatch_mutex`. `dispatch_batch` (`_factory.py:655`) spawns concurrently via `asyncio.gather`.
- **Why it matters:** Two agents claiming/editing the same task: wasted spend, conflicting commits.
- **Minimal fix:** Partial unique index on `agent_runs(task_id) WHERE status IN ('pending','running')`; convert the gate to insert-or-detect.
- **Confidence:** high (mechanism); med (frequency)

### [IMPORTANT] `kill_agent` infers self-termination from parameter shape — killing a child by `session_id` records a false `success`
- **Where:** `src/gobby/mcp_proxy/tools/agents.py:586-639` (esp. 621, 626, 629-639)
- **Failure mode:** `is_self_termination = resolved_session_id is not None` — no check that the session is the *caller's*. A parent killing a stuck child via session ref gets `effective_status="success"`; `_complete_self_terminated_run` marks the run success and notifies subscribers with a completion message. `recover_tasks_from_terminal_agents` only recovers non-success runs, so the dead child's task claim is never released.
- **Why it matters:** Wrong terminal state recorded; bogus "Agent completed" notifications; task stays claimed by an expired session indefinitely.
- **Minimal fix:** Treat explicit `session_id` as self-termination only when it resolves to `get_current_session_id()`; default `effective_status` to `"cancelled"` otherwise; validate `status` against the allowed set.
- **Confidence:** med — path verified; impact depends on callers using session refs for child kills.

### [IMPORTANT] Reused worktree/clone spawns silently lose the isolation context prompt
- **Where:** `src/gobby/mcp_proxy/tools/spawn_agent/_worktree_reuse.py:69`, `_implementation.py:590, 631`
- **Failure mode:** Reuse paths return `get_isolation_handler("none")`; `NoneIsolationHandler.build_context_prompt` returns the prompt unchanged (`agents/isolation.py:164-166`), so the "CRITICAL: Worktree Context" block (branch, paths) is never prepended — unlike fresh worktree spawns (`isolation.py:357-376`).
- **Why it matters:** Agents in reused worktrees assume they're in the main repo — wrong branch assumptions, the exact failure the warning exists to prevent.
- **Minimal fix:** Build the context prompt with the matching real handler (or construct the warning from `isolation_ctx`) while keeping the "none" handler for environment lifecycle.
- **Confidence:** high

### [IMPORTANT] Blocking `subprocess.run` on the event loop in async terminal cleanup
- **Where:** `src/gobby/mcp_proxy/tools/agents.py:126-140`
- **Failure mode:** `_cleanup_terminal_artifacts` is `async def` but kills tmux sessions with sync `subprocess.run(..., timeout=5)` — up to 5s of loop stall per kill, on every `stop_agent`/`kill_agent`/`end_agent_run` and each `cancel_stale_helpers` iteration.
- **Minimal fix:** `asyncio.create_subprocess_exec` + `wait_for`, or the already-async `TmuxSessionManager.kill_session`, or `asyncio.to_thread`.
- **Confidence:** high

### [IMPORTANT] Task resolution failure silently spawns an unbound agent
- **Where:** `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:483-495` (dependent gates at 497-511, 843-872)
- **Failure mode:** If task resolution raises, the except logs a warning and continues with `resolved_task_id = None`: no dedup gate, no actionability refusal, run row `task_id = NULL`, no auto-claim, no `assigned_task_id` injection. Success response carries no task field, so the caller can't tell binding failed.
- **Minimal fix:** Return a structured error from the except block (or at minimum `"task_binding": "failed"` in the response).
- **Confidence:** high (mechanism); med (severity)

### [IMPORTANT] `deliver_pending_messages` marks messages delivered before they're safely returned
- **Where:** `src/gobby/mcp_proxy/tools/agent_messaging.py:264-267`
- **Failure mode:** Per-message `mark_delivered` precedes payload construction; a failure on message N returns `success: False` with zero messages while 1..N-1 are flagged delivered and never fetched again.
- **Minimal fix:** Build all payloads first, then mark delivered in one transaction/batch; on partial failure return the marked subset.
- **Confidence:** med — loss mechanism certain; probability gated on failure rates.

- **[NIT] Registry merge reaches into private `_tools`** — `agents.py:952-953`; add a public merge API on `InternalToolRegistry`.
- **[NIT] `unregister_agent` says "mark cancelled" but records status `error`** — `agents.py:806-825`; pollutes error metrics.
- **[NIT] Synthetic STOP event hardcodes `SessionSource.CLAUDE`** — `agents.py:90`; killed gemini/codex agents fire stop rules tagged as Claude sessions.
- **[NIT] Magic error-string coupling `"No target PID found"`** — `agents.py:205`, `agent_cancellation.py:168` string-match a literal from `agents/kill.py:304`; use a structured code.
- **[NIT] `cancel_stale_helpers` doesn't catch `ValueError` from session resolution** — `agents.py:451`; loses the structured error shape siblings return.
- **[NIT] Reserved-`_` variable filter not applied to agent-definition variables** — `apply_persona.py:280-281` (filter exists at 74-77); same gap in `spawn_agent/_factory.py:383-384`.
- **[NIT] Unused `cli_source` parameter** — `apply_persona.py:156-162`.
- **[NIT] `kill_agent` `status`/`stop` contract drift** — `agents.py:626-652`: unknown `status` silently behaves as cancelled; `stop=False` ignored on the success path.
- **[NIT] Files at the monolith cap** — `spawn_agent/_implementation.py` (998 lines), `agents.py` (990 lines); next small change trips the 1,000-line rule.

### Merge & clones (`merge*.py`, `clones.py`)

### [BLOCKER] All clone tools run blocking git subprocesses directly on the event loop
- **Where:** `src/gobby/mcp_proxy/tools/clones.py:101, 107, 125, 323, 402, 486, 506, 511, 517, 526, 533, 540, 901`
- **Failure mode:** Every handler is `async def` (verified: lines 62-860), awaited inline by `InternalToolRegistry.call` (`internal.py:280-283`), but every git operation is sync `subprocess.run` (`clones/git.py:116-124`). `merge_clone`'s fetch alone allows `timeout=120`; clone/pull/push similar. While any runs, the entire daemon (HTTP, WS, all MCP traffic, hooks) is frozen.
- **Why it matters:** `merge.py` wraps identical calls in `asyncio.to_thread`; `clones.py` never does. A single `create_clone` of a large repo stalls the daemon for minutes under normal use.
- **Minimal fix:** Wrap every `git_manager.*` call in `await asyncio.to_thread(...)`, mirroring `merge.py`.
- **Confidence:** high (synthesizer re-verified)

### [IMPORTANT] No per-resolution lock in `merge_start`/`merge_apply` — concurrent git merges in the same worktree
- **Where:** `src/gobby/mcp_proxy/tools/merge.py:381-386`, `:585-761`; lock exists only in `merge_resolve` (`:517`)
- **Failure mode:** `merge_resolver.resolve` runs `git merge --no-commit --no-ff` in the worktree. Two concurrent `merge_start` calls interleave: the second finds the existing `pending` resolution with no conflict rows, adopts it (`:326`), and both run `git merge` in the same worktree. `merge_apply` writes/stages/commits with no lock.
- **Why it matters:** Concurrent merges corrupt `MERGE_HEAD`/index; resolution rows race.
- **Minimal fix:** Acquire `try_acquire_resolve_lock(resolution.id)` (release in `finally`) around the resolve phase of `merge_start` and the apply phase of `merge_apply`.
- **Confidence:** high

### [IMPORTANT] `merge_clone`: unhandled `TimeoutExpired` leaves clone stuck in `"syncing"`
- **Where:** `src/gobby/mcp_proxy/tools/clones.py:484-521` (finally block `:533-543`)
- **Failure mode:** `mark_syncing` at 484, then fetch/stash commands can raise `subprocess.TimeoutExpired` (re-raised by `clones/git.py:126-128`) with no try/finally resetting status. Clone stays `syncing` indefinitely. `branch -D`/`stash pop` in the finally can also raise and mask the merge result.
- **Minimal fix:** try/finally resetting status (as `sync_clone` does at 420-424); catch `TimeoutExpired`/`OSError` around fetch/stash/cleanup.
- **Confidence:** high

### [IMPORTANT] `delete_clone` rollback mints a new ID and drops fields; DB-before-files ordering orphans disk state
- **Where:** `src/gobby/mcp_proxy/tools/clones.py:312-348`
- **Failure mode:** DB row deleted first (317); if file deletion fails, "rollback" `clone_storage.create(...)` (331-338) mints a **new clone ID** and loses `agent_session_id`, `status`, `cleanup_after`, `last_sync_at`. Crash between 317 and 323 orphans the directory with no record.
- **Why it matters:** Violates atomic path+ID pair contract; claimed clones lose their claim; ID-based automation breaks.
- **Minimal fix:** Delete files first, then the DB row — or mark `cleanup` first and delete the row only after file deletion succeeds.
- **Confidence:** high

### [IMPORTANT] `create_clone` partial failure leaves an orphan clone directory
- **Where:** `src/gobby/mcp_proxy/tools/clones.py:96-159`
- **Failure mode:** After the clone lands on disk, `checkout -b` (107-111), `_resolve_task_id` (139), or `clone_storage.create` (142) can fail → error returned, directory stays, untracked; retry with same path collides.
- **Minimal fix:** Resolve `task_id` before cloning; delete the just-created directory on post-clone failure.
- **Confidence:** high

### [IMPORTANT] `claim_clone` is check-then-act; storage `claim()` is an unconditional UPDATE
- **Where:** `src/gobby/mcp_proxy/tools/clones.py:626-634`; `src/gobby/storage/clones.py:394-405`
- **Failure mode:** Ownership check and claim are separate statements with no conditional WHERE. Any second writer (other process, HTTP route, background job) interleaves; both sessions told they own the clone. Becomes intra-daemon the moment the blocking-IO Blocker is fixed with `to_thread`.
- **Minimal fix:** Conditional `UPDATE ... WHERE id = %s AND (agent_session_id IS NULL OR agent_session_id = %s)`, rowcount 0 = already claimed.
- **Confidence:** med

### [IMPORTANT] `_complete_direct_merge`: detached HEAD never restored (silent no-op `checkout HEAD`)
- **Where:** `src/gobby/mcp_proxy/tools/merge.py:156-159, 222-229`
- **Failure mode:** On detached HEAD, `rev-parse --abbrev-ref HEAD` returns literal `"HEAD"`; the finally runs `git checkout HEAD` — a successful no-op. Repo silently left on `target_branch`; original position lost; the restore-failure warning (230-235) never fires.
- **Minimal fix:** When `original_branch == "HEAD"`, capture the SHA and restore via `checkout --detach <sha>` (or refuse direct merge from detached state).
- **Confidence:** high

### [IMPORTANT] Direct merge commits to target, then reports `success: False` if the tree is dirty — DB left `pending` though the merge landed
- **Where:** `src/gobby/mcp_proxy/tools/merge.py:205-214` (analogous MERGE_HEAD path `:742-745`)
- **Failure mode:** The dirty check runs *after* the merge commit exists. Pre-existing unrelated dirt → `success: False`; `merge_apply` (705-707) returns before `update_resolution(status="resolved")`. Git has the merge; DB says pending; retries keep failing.
- **Minimal fix:** Clean-tree check *before* checkout/merge; if kept post-merge, still mark resolved when `merge_sha` exists and report dirt as a warning.
- **Confidence:** med

### [IMPORTANT] `probe_branch_protection`: worktree `base_branch` fallback is dead code — probes `main` instead
- **Where:** `src/gobby/mcp_proxy/tools/merge.py:843, 852`
- **Failure mode:** `branch` defaults to `"main"`, so `branch = branch or worktree.base_branch` never picks up the worktree's base. Worktrees based on `develop`/`release` get `main`'s protection verdict.
- **Minimal fix:** `branch: str | None = None`; apply worktree fallback, then default `"main"`.
- **Confidence:** high

### [IMPORTANT] `verify_in_worktree` timeout kills only the direct child — test-runner trees leak
- **Where:** `src/gobby/mcp_proxy/tools/merge_landscape.py:647-658`
- **Failure mode:** No `start_new_session=True`; `proc.kill()` SIGKILLs the runner only. Forked workers (`pytest -n`, jest) survive, keep ports/locks, keep writing into the worktree after `timed_out` — corrupting the clean-tree gate this tool implements.
- **Minimal fix:** `start_new_session=True` + `os.killpg(...)` on timeout.
- **Confidence:** med

### [IMPORTANT] `cleanup_stale_clones` deletes clone files but never updates the DB record
- **Where:** `src/gobby/mcp_proxy/tools/clones.py:900-906`
- **Failure mode:** With `delete_files=True, dry_run=False`, the directory is removed but the row (just marked `stale`) is never deleted/marked. DB permanently holds rows whose `clone_path` doesn't exist.
- **Minimal fix:** On successful file deletion, `clone_storage.delete(c.id)` (or `mark_cleanup`).
- **Confidence:** high

### [IMPORTANT] `_active_merge_resolution_payload` swallows all DB errors at debug level
- **Where:** `src/gobby/mcp_proxy/tools/merge_landscape.py:813-819`
- **Failure mode:** `except Exception … logger.debug` around `create_conflict`. The intended duplicate-row race is handled correctly elsewhere with `except psycopg.IntegrityError` (`merge_conflict_hydration.py:107-108`); here real failures are invisible and `inspect_merge_state` under-reports conflicts.
- **Minimal fix:** Catch `psycopg.IntegrityError` at debug; log other `psycopg.Error` at warning.
- **Confidence:** high

### [IMPORTANT] `merge_clone` stash push/pop on the shared main repo is unserialized and pops LIFO
- **Where:** `src/gobby/mcp_proxy/tools/clones.py:505-548` (with `merge_branch` checking out target in the main repo, `clones/git.py:718-729`)
- **Failure mode:** stash-push `.gobby/` → checkout → merge → checkout back → `stash pop` mutates the user's primary repo with no lock; `stash pop` takes the newest stash, not necessarily this call's. Interleaved stashers get wrong stashes popped; pop failure is only a log warning, stranding user changes.
- **Minimal fix:** Pop by exact recorded stash ref; serialize main-repo merge operations behind one lock shared with `_complete_direct_merge`.
- **Confidence:** med

- **[NIT] `_MERGE_RESOLVE_LOCKS` grows unbounded** — `merge_resolve_locks.py:7-15`; one `asyncio.Lock` leaked per resolution forever.
- **[NIT] `collect_git_conflicts` fallback subprocess has no timeout** — `merge_conflict_hydration.py:36-45`.
- **[NIT] `cherry_pick_into_worktree` passes commits without `--` or a mid-operation guard** — `merge_landscape.py:516-521`; option-shaped "commits" change semantics.
- **[NIT] `merge_clone` docstring contradicts implementation** — `clones.py:456-458` vs `:482-487` (says push-to-remote; code fetches from local clone path).
- **[NIT] `create_clone` logs failures without traceback** — `clones.py:157-158`; no `exc_info=True`.
- **[NIT] Redundant manual arg coercion with uncaught `ValueError`** — `clones.py:813-814, 876-882`; registry already coerces per schema.
- **[NIT] `_resolve_task_id` exceptions escape `get_clone_by_task`/`link_task_to_clone`** — `clones.py:702, 744`; raw raise instead of the `{"success": False}` shape.

### Misc registries (`memory*.py`, `hub.py`, `metrics.py`, `wiki.py`, `config.py`, `internal.py`, `communications.py`, `voice.py`, `review_learning.py`, `profiles.py`, `artifacts.py`, `cron.py`, `build.py`)

### [IMPORTANT] `set_config_batch` is not atomic for non-secret batches despite documented atomicity
- **Where:** `src/gobby/mcp_proxy/tools/config.py:353-366` (persist), docstring `:275-288`; `storage/config_store.py:141-160` (`set_many`)
- **Failure mode:** `set_many` loops one `db.execute` per key — each its own transaction. Only the secrets branch wraps in `db.transaction()` (`:359`). Failure on key N leaves keys 1..N-1 committed while in-memory config is never updated; partial state loads on next restart. `ensure_defaults` (`:535`) has the same unwrapped call.
- **Why it matters:** The batch API exists precisely for sections where partial writes produce invalid config.
- **Minimal fix:** Wrap the plain-updates branch in `with db.transaction():` (or move the transaction into `ConfigStore.set_many`).
- **Confidence:** high

### [IMPORTANT] `create_cron_job` accepts invalid/missing schedules, reports success, job silently never runs
- **Where:** `src/gobby/mcp_proxy/tools/cron.py:133-152`; root cause `storage/cron.py:88-96`, `:157-176`, `:583-591`
- **Failure mode:** Invalid `cron_expr` (or malformed/past `run_at`) → `compute_next_run` swallows `ValueError/KeyError` → row persists with `next_run_at = NULL` → `get_due_jobs` (`next_run_at IS NOT NULL`) never selects it. Tool returns success with no warning.
- **Minimal fix:** After `compute_next_run`, error when `enabled=True` and `next_run_at` is None for cron/once schedules.
- **Confidence:** high

### [IMPORTANT] `update_cron_job` never recomputes `next_run_at` and cannot re-enable toggled-off jobs
- **Where:** `src/gobby/mcp_proxy/tools/cron.py:178-230`; root cause `storage/cron.py:342-373`
- **Failure mode:** (a) Schedule-field updates leave the stored `next_run_at` untouched — old fire time, or permanent dormancy if it was NULL (scheduler only recomputes after a dispatch). (b) `update_cron_job(job_id, enabled=True)` always fails for jobs disabled via toggle (toggle sets `next_run_at=None`; `update_job` raises "enabled=True requires next_run_at"; the MCP tool doesn't expose `next_run_at`).
- **Minimal fix:** Recompute `next_run_at` via `compute_next_run` whenever schedule fields or `enabled` change and the caller didn't pass it.
- **Confidence:** high

### [IMPORTANT] `delete_memory` / `update_memory` have no project scoping (cross-project mutation)
- **Where:** `src/gobby/mcp_proxy/tools/memory.py:356-370`, `:505-534`; contrast `get_memory` at `:431`; manager signatures `memory/manager.py:490`, `:624`
- **Failure mode:** A session in project A can delete or rewrite any memory in project B by ID. The read path is project-scoped; the destructive paths are not — you can't even `get` the memory you're about to delete to confirm it.
- **Minimal fix:** Thread `project_id=get_current_project_id()` through scoped manager variants; not-found on mismatch.
- **Confidence:** high

### [IMPORTANT] Background memory-dream slot leaks when `start_async` raises, permanently exhausting the cap
- **Where:** `src/gobby/mcp_proxy/tools/memory_dream.py:135-144`
- **Failure mode:** Slot acquired at 135; `start_async` (DB insert, can raise) at 140 and `started["run_id"]` (KeyError) at 144 release nothing on failure. After 4 failures, every `memory_dream(wait=False)` returns "limit reached (4)" until daemon restart. Tests cover the cap but not `start_async` raising.
- **Minimal fix:** try/except around 140-144 releasing the slot before re-raising.
- **Confidence:** high

### [IMPORTANT] Sync internal tools run blocking DB I/O directly on the event loop (systemic)
- **Where:** dispatch point `src/gobby/mcp_proxy/tools/internal.py:281-284` (sync `tool.func(**args)` inline) via `services/tool_execution.py:369`; examples: all of `metrics.py`, `cron.py`, `communications.py`, `config.py:195-271`, `voice.py:55-68`, `profiles.py:32-56`, `memory.py:376-417/423-452/540-548`
- **Failure mode:** Every sync tool body executes on the proxy's loop, so each Postgres round-trip stalls the daemon. A slow/locked query (`cleanup_old_metrics` on a large table) freezes everything for its duration. `hub.py:95-106` demonstrates the sanctioned offload (`run_db`/`to_thread`); the rest is drift.
- **Minimal fix:** Offload non-coroutine tool funcs via `asyncio.to_thread` in `InternalToolRegistry.call` — one fix covers the whole directory.
- **Confidence:** high (mechanics); med (operational impact)

### [IMPORTANT] Secret masking is name-based while encryption is flag-based — explicit secrets readable in plaintext
- **Where:** `src/gobby/mcp_proxy/tools/config.py:54-57` (`_mask_secret_value`), applied `:153-158`, `:174-185`; suffix list `storage/config_store.py:29-41`
- **Failure mode:** `set_config(key, value, is_secret=True)` encrypts at rest, but read-side masking only suffix-matches key names (`api_key/_secret/password/...`). Keys ending `token`, `credential`, `dsn`, `_url` are stored encrypted yet returned in plaintext by `get_config`/`get_config_section`. `delete_config` consults the DB flag (`:410-411`); the read path alone ignores it.
- **Minimal fix:** Mask keys present in `config_store.get_secret_keys()` too.
- **Confidence:** med

### [IMPORTANT] `reset_metrics` with no arguments wipes all metrics globally; negative retention deletes everything
- **Where:** `src/gobby/mcp_proxy/tools/metrics.py:186-210`, `:248-270`; store fall-through in `mcp_proxy/metrics_store.py` (`DELETE FROM tool_metrics` with no WHERE; `retention_days` unbounded)
- **Failure mode:** All filters optional → `reset_metrics()` deletes every row across all projects; `cleanup_old_metrics(retention_days=-1)` computes a future cutoff and deletes everything.
- **Minimal fix:** Require at least one filter; reject `retention_days < 1`.
- **Confidence:** high (behavior); med (severity — metrics are regenerable)

### [IMPORTANT] `show_file` reports success when the artifact broadcaster is not wired (silent no-op)
- **Where:** `src/gobby/mcp_proxy/tools/artifacts.py:194-211`
- **Failure mode:** `if bc: await bc(...)` — broadcaster None → broadcast skipped, tool still returns success. Agent believes the user saw the file. `tests/mcp_proxy/tools/test_artifacts.py:50-65` codifies the behavior.
- **Minimal fix:** Error, or an explicit `broadcast: false` field, when the broadcaster is None.
- **Confidence:** high (behavior); med (severity)

- **[NIT] Hardcoded one-off speculative-memory heuristic** — `memory.py:66-133`; incident-specific content fingerprint returning a hardcoded task title.
- **[NIT] Semaphore-recreate helper only fires under test monkeypatching** — `memory_dream.py:69-74`; dead in production, forgets outstanding permits.
- **[NIT] `_prepare_call` silently drops unknown arguments** — `internal.py:309-317`; typo'd params vanish instead of erroring (this silent drop upgrades schema drift elsewhere into wrong-data writes — see tasks/sessions Blocker/Important).
- **[NIT] `profiles.py` uses `ok` envelopes and raises raw exceptions** — `profiles.py:40,71,118,157`; inconsistent caller contract.
- **[NIT] `voice.py` `_persist` dumps config without `by_alias=True`** — `voice.py:64`; works only via `populate_by_name`.
- **[NIT] `unlink_identity` returns success for nonexistent IDs** — `communications.py:219-224`; rowcount ignored (`storage/communications.py:192-198`).
- **[NIT] `_structured_result` discards non-dict payloads** — `wiki.py:342-345`; list payload replaced with `{}` under `success: true`.
- **[NIT] System-project filter matches by name prefix** — `hub.py:185-187`; a real project named `_personal-site` is hidden.
- **[NIT] Identity function `_storage_config_key_to_public_key`** — `config.py:83-85`; dead abstraction.

### Tasks subpackage (`tasks/`)

### [BLOCKER] `link_task_to_session` advertises a `session_id` parameter that is silently discarded
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_session.py:29-32` (signature), `:59` (description), `:66-71` (schema)
- **Failure mode:** Schema/description declare `session_id`; the handler signature is `link_task_to_session(task_id, action)`. `_prepare_call` (`internal.py:294-318`) filters unknown args, so `session_id=X` silently links the task to the **current** session, returning success. Verified by synthesizer.
- **Why it matters:** Silent wrong-row write to `session_tasks`; orchestrators linking tasks to child/peer sessions corrupt attribution. Tests cover only the no-arg path.
- **Minimal fix:** Add `session_id: str | None = None` to the handler and resolve it when provided — or delete it from the schema.
- **Confidence:** high

### [IMPORTANT] Task claim is read-check-write with no atomic guard — two claimants can both succeed
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_lifecycle_claim.py:92-152`; root cause `storage/tasks/_transitions.py:133-154`
- **Failure mode:** Storage `claim_task` = `get_task` → owner check → unconditional UPDATE, no transaction or conditional WHERE. The MCP pre-check is also non-atomic, and `delegated_claim` from the stale read passes `force=True` (`:151`), stomping an owner that claimed in between. Multiple claim surfaces exist (HTTP routes, `agent_spawn.py:267,373`, dispatcher); any off-loop execution interleaves.
- **Why it matters:** "Two sessions claim the same task" is the exact hazard the tool's conflict detection advertises against; it survives only by event-loop serialization.
- **Minimal fix:** Single conditional UPDATE with `WHERE ... claimed_by_session_id IS NULL OR = caller`, raising `TaskAlreadyClaimedError` on 0 rows.
- **Confidence:** med

### [IMPORTANT] `update_task` bypasses the category='code' ⇒ validation_criteria invariant
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_crud.py:488-489`, `:474-475` vs create-side `:115-124`
- **Failure mode:** `create_task` rejects code tasks without criteria/domain; `update_task` sets `category="code"` with no check and accepts `validation_criteria=""`. A code task without criteria skips LLM validation on close (`_lifecycle_close.py:270`).
- **Minimal fix:** Compute effective post-update category/criteria/domain and enforce the same invariant as create.
- **Confidence:** high

### [IMPORTANT] `cancel_expansion_run` overwrites terminal state of completed/failed runs
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_expansion.py:700-714`; `storage/expansion_runs.py:392-403`
- **Failure mode:** No status check before `cancel`; storage unconditionally writes `status='cancelled'` + error + `completed_at` — rewriting history of a completed (possibly auto-applied) run. `resume_expansion_run` guards terminal state (`:666`); cancel doesn't.
- **Minimal fix:** Fetch the run; no-op idempotently when already terminal.
- **Confidence:** high

### [IMPORTANT] Dispatch mutex released before stage-transition validation across all stage tools
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py:280-285, 323-328, 446-453, 461-467, 588-594, 631-637`; `_stage_review.py:266-270, 353-357, 459-463`
- **Failure mode:** `_release_current_agent_dispatch_mutex` (and commit auto-link) run *before* the transition, which can raise `IllegalStageTransitionError`. On failure the spawn lease is gone, the stage row unchanged, the agent still running — next dispatcher heartbeat can spawn a second agent on the same task.
- **Why it matters:** Mutex contract is "release only after the protected work concludes"; this creates a double-dispatch window.
- **Minimal fix:** Move release (and auto-link) after the successful transition.
- **Confidence:** med

### [IMPORTANT] Blocking subprocess/network calls on the daemon event loop (tasks delivery/affected-files)
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_delivery.py:368-379, 395-404, 408-419` (sync `git push`, timeout=60, inside `async def open_delivery_pr`); `_affected_files.py:110-116` (`git diff-tree` per linked commit)
- **Failure mode:** Registry calls these inline on the loop; a slow `git push` freezes the daemon up to 60s; `update_observed_files` blocks 10s × N commits. `_stage_review.py:176-189` shows the correct executor pattern.
- **Minimal fix:** `run_in_executor`/`to_thread` for the subprocess calls.
- **Confidence:** high

### [IMPORTANT] Delivery campaign written before stage transition — related state not atomic
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py:433-445` (campaign → ready_to_merge/blocked before approve/reject at `:454-474`), `:624-630` (campaign → merged before complete_stage at `:640-645`)
- **Failure mode:** A failing transition leaves the campaign claiming `ready_to_merge`/`merged` while the manifest disagrees; raw exception, no structured hint that half landed.
- **Minimal fix:** Transition first, record campaign state after (or one transaction at the manager level).
- **Confidence:** med

### [IMPORTANT] "Read-only" stage registry on gobby-tasks exposes four mutating tools
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_stage_read.py:1` (docstring), `:159-211`, `:213-284`; merged at `_factory.py:91-92`
- **Failure mode:** `update_stage`/`restore_stage`/`delete_stage`/`set_task_type_defaults` ship on the hot-path gobby-tasks server, contradicting the module docstring, factory comment, and the gobby-tasks/gobby-tasks-ops split (`_ops_factory.py:1-15`).
- **Minimal fix:** Move them to `_stage_ops.py` (gobby-tasks-ops) or document the placement decision.
- **Confidence:** med

### [IMPORTANT] `update_stage` crashes on unknown update keys; registry mutations return raw exceptions
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_stage_read.py:159-162, 179-182, 196-199`; `storage/tasks/_stage_registry.py:167-178, 241-246`
- **Failure mode:** Free-form `updates` object → `StageRegistryEntry(**payload)` raises `TypeError` on unknown keys, uncaught; unknown stage names raise `ValueError` — opaque proxy errors instead of the structured envelope siblings return.
- **Minimal fix:** Validate keys against the dataclass fields; wrap in try/except returning structured errors.
- **Confidence:** high

### [IMPORTANT] `resolve_project_from_session` swallows all errors and silently falls back to another project
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_context.py:157-168` (`except Exception: pass`)
- **Failure mode:** Any session-resolution failure (transient DB error) silently falls back to daemon-CWD project or `PERSONAL_PROJECT_ID`; `create_task` (`_crud.py:103`) then creates the task in the wrong project with success returned.
- **Minimal fix:** Catch specific errors, log at warning, fail the call when a session ref was provided but didn't resolve.
- **Confidence:** med

### [IMPORTANT] `_session_id` falls back to the unresolved session ref on resolution failure
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py:39-46`
- **Failure mode:** `except Exception: return session_ref` persists raw refs (`#3`, prefixes) as `by_session_id` in stage audit columns; `_dispatch_mutex_release.py:52-62` compares against `agent_runs.child_session_id` where a non-UUID never matches — lease silently not released, garbage audit refs. Contrast `_stage_review._resolve_session` (`:48-58`) which errors properly.
- **Minimal fix:** Return None (or structured error) on resolution failure.
- **Confidence:** med

### [IMPORTANT] Fire-and-forget `loop.create_task` without strong references; relay catches only DB errors
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_notifications.py:24-28`; `_stage_review.py:223-234`, `:190-195`
- **Failure mode:** Tasks spawned without retained references can be GC'd mid-execution, dropping parent progress notifications and coordinator signoff relays. `_relay_signoff_to_build_coordinator` catches only DB errors; anything else is an unhandled task exception. `_expansion.py:61-73` shows the correct registry+done-callback idiom.
- **Minimal fix:** Module-level task set with done-callback discard; broaden relay exception logging.
- **Confidence:** med

### [IMPORTANT] `close_task` falls back to `cwd="."` (daemon CWD) for git operations
- **Where:** `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py:119`
- **Failure mode:** When `resolve_task_repo_path` returns None, `link_commit` (`:124`), claim-window auto-link (`:178-184`), SHA normalization and `rev-parse HEAD` (`:317-327`) run against whatever repo the daemon sits in — wrong-repo commits linked and recorded as `closed_commit_sha`.
- **Minimal fix:** Structured error when repo path is None and commit operations are required.
- **Confidence:** med

- **[NIT] Dead escalation branch in close path** — `_lifecycle_validation.py:458-478` ignores two of three params, always `route_to_escalation=False`; 33 unreachable lines in `_lifecycle_close.py:334-366`.
- **[NIT] `get_task` N+1 per dependency in brief mode** — `_crud.py:388-404`.
- **[NIT] `except Exception` mislabels DB failures as "Invalid parent_task_id"** — `_search.py:80-85`.
- **[NIT] Always-true `if session_id:` re-check** — `_lifecycle_status.py:200, 314`.
- **[NIT] Legacy `sqlite3.DatabaseError` catch in Postgres-only hub** — `_stage_review.py:8, 190`.
- **[NIT] Four silent `except Exception: return False` blocks without logging** — `_dispatch_mutex_release.py:20-43`; lease-release failures undiagnosable.
- **[NIT] `update_observed_files` silently skips failed/empty commits** — `_affected_files.py:117-121`; counts understate.
- **[NIT] Docstring drift** — `_lifecycle_close.py:4` (mentions removed worktree status updates); `_lifecycle_claim.py:72-73` (return shape wrong).

### Top-level task modules (`task_readiness.py`, `task_validation.py`, `task_sync.py`, `task_github.py`, `task_repo_paths.py`, `task_dependencies.py`)

### [IMPORTANT] Sync git/`gh` subprocess tools block the MCP proxy event loop
- **Where:** `src/gobby/mcp_proxy/tools/task_github.py:63` (`gh issue list`, network, timeout=30); `task_sync.py:116, 171, 244, 310` (git via `link_commit`/`normalize_commit_sha` and injected fns)
- **Failure mode:** Registered as sync funcs, run inline on the loop (`internal.py:281-283`). One slow `gh` network call stalls every concurrent session for up to 30s.
- **Minimal fix:** `async def` + `asyncio.to_thread`, matching `merge.py`.
- **Confidence:** high

### [IMPORTANT] `import_github_issues` reports success while silently dropping requested parent nesting
- **Where:** `src/gobby/mcp_proxy/tools/task_github.py:153-168`
- **Failure mode:** Unresolvable `parent_task_id` → warning logged, task still appended to `imported`, `success: True`. Since the parent is the same for every issue, all imported tasks silently land at root.
- **Minimal fix:** Resolve the parent once before the loop and fail fast (or surface per-issue `parent_errors`).
- **Confidence:** high

### [IMPORTANT] Silent truncation in readiness/parent-completion aggregation yields wrong dispatch decisions
- **Where:** `src/gobby/mcp_proxy/tools/task_readiness.py:66-70, 254` (`limit=200`); `task_validation.py:100` (`limit=1000`)
- **Failure mode:** `_get_ready_descendants` pulls ≤200 ready tasks then filters to descendants — epics with >200 ready tasks elsewhere can report "no ready descendants" (wrongly blocked → stalls dispatch). `validate_task`'s parent branch computes `all_closed=True` from a capped child list — >1000 children can wrongly validate and close an incomplete parent.
- **Minimal fix:** Paginate until exhausted, or COUNT open children for the parent check.
- **Confidence:** med (needs large hierarchies to trigger)

- **[NIT] `is_descendant_of` is dead code with a latent unhandled `ValueError`** — `task_readiness.py:92-126`; no callers; `get_task` raises rather than returning None, so the `if not task` guard is unreachable.
- **[NIT] Redundant `current_fail_count` recomputation** — `task_validation.py:164, 197`.
- **[NIT] Broad `except Exception` in GitHub fetch** — `task_github.py:119, 159`; catch `RuntimeError`/`TimeoutExpired`/`JSONDecodeError` explicitly.
- **[NIT] `_resolve_ready_tasks` unguarded `get_task` on resolved parent** — `task_readiness.py:251, 264`; narrow deletion race raises raw `ValueError`.

Positive note: `task_repo_paths.py` is solid — `O_NOFOLLOW` + per-component stat + inode containment resists symlink swaps; tests exist.

### Workflows & plans (`workflows/`, `plans/`)

### [BLOCKER] `cancel_pipeline` neither stops the running execution nor kills the right agents
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py:154-235` (kill at 189-207, status writes 209-228)
- **Failure mode:** Three compounding defects, all verified: (1) the background asyncio task driving the execution (`_background_tasks`, created `:295-307`/`:429-441`) is never cancelled, and the executor's step loop never re-reads execution status mid-run (terminal-status check exists only at resume entry, `pipeline_executor.py:287-299`) — after "cancel" it keeps executing and finally writes COMPLETED (`pipeline_executor.py:574`), overwriting CANCELLED. (2) The agent kill targets `arm.list_by_parent(execution.session_id)` — the *caller* session; top-level pipelines run steps under a child session (`pipeline-{execution.id}`), so the pipeline's own agents are missed. (3) The same query SIGKILLs every running/pending agent the caller session spawned, including agents from other pipelines or direct spawns.
- **Why it matters:** Cancel returns success while side effects continue; final status corrupts to COMPLETED; unrelated agents are killed. No test exists for `cancel_pipeline`.
- **Minimal fix:** Cancel the matching `_background_tasks` entry (index by execution_id); have the executor check execution status between steps and abort on CANCELLED; kill agents parented to the pipeline child session.
- **Confidence:** high (synthesizer re-verified the no-mid-run-check and kill-target claims)

### [IMPORTANT] Approval tokens exposed to the gated agent and never invalidated
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py:665` (`resume_token`), `:689` (`approval_token`) in `get_pipeline_status`
- **Failure mode:** `get_pipeline_status` returns live approval tokens to any caller — including the agent paused on a human-approval gate, which can then `approve_pipeline(token)` itself. Tokens are never cleared or status-checked after use: `get_step_by_approval_token` has no status filter (`storage/pipelines.py:677-690`); `approve_step`/`reject_step` don't verify WAITING_APPROVAL — tokens are reusable, and a stale `reject_pipeline` flips a COMPLETED step to FAILED.
- **Why it matters:** The approval gate is bypassable by the party it gates; reuse corrupts execution state.
- **Minimal fix:** Redact tokens from status output; reject approve/reject when the step is not WAITING_APPROVAL.
- **Confidence:** high (mechanics); med (intent)

### [IMPORTANT] `enabled` is decorative for pipelines — disabled pipelines still run and are exposed as tools
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py:276`; `_pipelines.py:541-561`
- **Failure mode:** Nothing in the execution path checks `enabled`: `WorkflowLoader._load_from_db` (`workflows/loader.py:92-149`) and `_merge_db_pipelines` (`loader_discovery.py:58-90`) ignore it; dynamic `pipeline:<name>` tools register for disabled pipelines; `import_from_yaml` defaults new definitions to `enabled=False` yet they run anyway.
- **Why it matters:** Violates the enable/disable + DB-source-of-truth contract — "disabling" a pipeline does nothing.
- **Minimal fix:** Filter `enabled=True` in the loader paths or error "pipeline disabled" in `run_pipeline`.
- **Confidence:** high

### [IMPORTANT] Project-scoped definitions unreachable: filesystem path passed where loader matches DB project UUID
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_query.py:41-47`; `_pipelines.py:177-183`; `_pipeline_execution.py:276` (no scope at all)
- **Failure mode:** Tools pass `get_workflow_project_path()` (a filesystem path) to loaders that do `project_id = str(project_path)` and query `WHERE project_id = %s` against rows storing project UUIDs. A pipeline created with `create_pipeline(..., project_id=<uuid>)` is invisible to `get_pipeline` and cannot be run at all (`get_by_name(project_id=None)` matches only NULL-project rows) — silent fallback to global instead of an error.
- **Minimal fix:** Resolve the project UUID from session/project context and pass it through run/get paths.
- **Confidence:** med-high

### [IMPORTANT] `import_workflow` writes a YAML file the runtime never reads
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_import.py:82-95`
- **Failure mode:** The loader is DB-only at runtime; `import_workflow` copies the file and clears cache but never syncs to DB, so `get_workflow`/run can't load it — while `list_workflows` (filesystem-merged) shows it. `reload_cache` (`:122-128`) syncs other types but not step workflows.
- **Minimal fix:** Import into the DB via `def_manager.import_from_yaml` after copying.
- **Confidence:** med-high

### [IMPORTANT] `update_workflow` yaml_content path bypasses type-specific validation; can rewrite rule/agent rows with junk and flip types
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_definitions.py:41-55`, `:180-204`
- **Failure mode:** For non-pipeline types, `_validate_yaml` validates against permissive `WorkflowDefinition` (defaults for everything) — `update_workflow` on a rule replaces `definition_json` without `RuleDefinitionBody` (which `update_rule` enforces, `_rules.py:217-221`); same for agents. YAML `type:` can silently convert a pipeline row to a rule (`:190-192`), bypassing `_require_pipeline` guards. Broken rule bodies fail at rule-engine parse time, silently disabling enforcement.
- **Minimal fix:** Dispatch validation by effective `workflow_type`; reject type changes via yaml_content.
- **Confidence:** high

### [IMPORTANT] `create_workflow` cannot create what it validates: step workflows pass validation then fail import
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_definitions.py:49-53` vs `:99-102`
- **Failure mode:** `_validate_yaml` accepts missing/`"step"` type; `import_from_yaml` rejects types outside `{rule, variable, agent, pipeline}` — the advertised "create a workflow" use case validates then errors.
- **Minimal fix:** Align accepted types; fix the tool description.
- **Confidence:** high

### [IMPORTANT] Unresolvable `project` ref silently widens plan mutations to all projects
- **Where:** `src/gobby/mcp_proxy/tools/plans/__init__.py:277-285` (`_optional_project_id`), used by `archive_plan` (:136-140), `delete_plan` (:208-213), `update_plan_hash`, `regenerate_coverage_manifest`, `get_plan`
- **Failure mode:** Provided-but-unresolvable `project` returns None; `_find_plan` drops the project clause (`storage/plans.py:275-290`), matching `plan_id` across **all** projects. `delete_plan` then hard-deletes a plan in a project the caller never named.
- **Minimal fix:** Error (`invalid_project`) when `project` was provided but didn't resolve.
- **Confidence:** high

### [IMPORTANT] `resume_pipeline` has no concurrency guard — duplicate executors for the same execution
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py:355-441` (check `:359-363`, reset `:410`, RUNNING write `:424-427`, spawn `:429-441`)
- **Failure mode:** Check-then-act: two concurrent resumes both observe FAILED, both reset steps, both spawn `_execute_pipeline_background` — interleaved step re-runs and double agent spawns.
- **Minimal fix:** Atomic `UPDATE ... SET status='running' WHERE id=%s AND status='failed'`, bail if rowcount 0.
- **Confidence:** high (window); med (frequency)

### [IMPORTANT] Workflow-scoped `set_variable` does a stale whole-row overwrite of the workflow instance
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_variables.py:133-144`
- **Failure mode:** `get_instance` → mutate → `save_instance` upserts the entire row including `current_step` and counters (`workflows/state_manager.py:39-76`). A hook-driven step transition landing between read and write is rolled back — workflow step regression; concurrent set_variable loses updates. The session-scoped path was already fixed (atomic `merge_variables`, `state_manager.py:205-207`); this one wasn't.
- **Minimal fix:** Atomic `merge_instance_variables` (`UPDATE ... SET variables = variables || %s::jsonb`).
- **Confidence:** med

### [IMPORTANT] `list_workflows` returns every definition type from every project
- **Where:** `src/gobby/mcp_proxy/tools/workflows/_query.py:130-151`
- **Failure mode:** `mgr.list_all(workflow_type=None)` with no project filter returns all rules/agents/variables/pipelines across all projects as "workflows"; documented filter values ("step"/"lifecycle") don't exist as DB types.
- **Minimal fix:** Constrain to actual workflow kinds and caller's project; fix documented values.
- **Confidence:** high (behavior); med (intended taxonomy)

- **[NIT] Stale `PipelineExecutionManager` Protocol** — `_pipeline_execution.py:72-75`; missing kwargs the real manager has; unchecked because call sites type as `Any`.
- **[NIT] Dead code** — `_variables.py:249-297` (`save_variable_template`, zero callers), `_pipeline_execution.py:408-409` (unreachable raise), `plans/__init__.py:242` (symmetry import).
- **[NIT] Daemon `Path.cwd()` used as project root** — `_definitions.py:300`, `plans/__init__.py:236-237`; template cleanup and relative `plan_file` resolve against daemon cwd, not the caller's project.
- **[NIT] Delete tools never receive `project_path`** — `workflows/__init__.py:307-314, 395-401, 509-515, 580-586`; documented YAML-template cleanup is a no-op for project files (vs `_rules.py:359-371` docstring).
- **[NIT] `create_rule` duplicate-name race and stored-tags asymmetry** — `_rules.py:288-306`; raw UNIQUE violation under concurrency; create keeps `tags` in `definition_json` while update strips them.
- **[NIT] `list_pipelines` description/payload drift** — `_pipelines.py:152-155`, `_pipeline_discovery.py:31-37`; says "directories", discovery is DB-only; payload omits `enabled`.
- **[NIT] gobby-plans error-envelope inconsistencies** — `plans/__init__.py:41` (raise outside try → generic envelope; wrong message when project given but unresolvable), `:208-218` (`delete_plan` alone doesn't catch `psycopg.Error`).
- **[NIT] Dynamic exposed-pipeline tools are startup-frozen** — `_pipelines.py:141-150, 523-561`; later-created/toggled pipelines don't appear until daemon restart.

### Sessions, skills & worktrees (`sessions/`, `skills/`, `worktrees/`)

### [BLOCKER] `cleanup_stale_worktrees` force-destroys uncommitted work and unmerged branches with no guard
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_cleanup.py:147-154` (stale loop), `:173-180` (expired loop)
- **Failure mode:** With `delete_git=True, dry_run=False`, every worktree idle for `hours` (default 24) gets `delete_worktree(force=True, delete_branch=True)` — `git branch -D` plus `shutil.rmtree` fallback (`worktrees/git/_lifecycle.py:184-187`). No per-worktree dirty check, no merged-state re-validation (unlike `delete_worktree`, which refuses dirty trees without force). Verified by synthesizer.
- **Why it matters:** Irreversible loss of working-tree changes; branch commits survive only in reflog. "Stale" = merely inactive — the normal state of parallel-agent worktrees.
- **Minimal fix:** Skip (and report) dirty worktrees in the stale loop; use `force=False` branch deletion (`-d` refuses unmerged) unless explicitly opted in; re-check `is_worktree_git_merged` in the expired loop.
- **Confidence:** high

### [BLOCKER] `create_worktree` failure cleanup force-deletes a pre-existing branch (`create_branch=False`)
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_create.py:129-141` (invalid task ref), `:153-165` (DB failure)
- **Failure mode:** Both cleanup paths call `delete_worktree(force=True, delete_branch=True)` unconditionally. Called with `create_branch=False` (param at `:45`) for an existing branch, a task-ref typo or DB insert failure `-D`s a branch the tool never created. Verified by synthesizer.
- **Why it matters:** Data loss triggered by a routine input error; cleanup should restore prior state, not exceed it.
- **Minimal fix:** Pass `delete_branch=create_branch` in both cleanup paths.
- **Confidence:** high

### [IMPORTANT] `merge_worktree` marks the worktree merged based on caller-supplied branches, chaining into deletion
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_sync.py:490-492`
- **Failure mode:** `mark_merged` fires when `effective_source` (`source_branch or worktree.branch_name`) is an ancestor of `merge_target` — with custom branch args, the worktree's own branch may be unmerged yet the row gets `status=merged` + `cleanup_after=now`. The next `cleanup_stale_worktrees(dry_run=False)` treats it as expired and force-deletes worktree and branch (never re-validated against git).
- **Minimal fix:** Only `mark_merged` when defaults were used; re-check git merge state in expired cleanup.
- **Confidence:** high (mislabel); med (frequency)

### [IMPORTANT] `merge_worktree` mutates the shared main checkout with no mutual exclusion
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_sync.py:215, 305-311, 402-407, 536-550`
- **Failure mode:** When no worktree holds the target branch, `merge_cwd` is the user's main checkout; checkout → stash → merge → restore run as separate awaited `to_thread` steps. Concurrent calls interleave — call B's checkout lands between A's checkout and A's merge → merge commit on the wrong branch. Daemon crash mid-sequence leaves the repo on the wrong branch with `.gobby/` stashed.
- **Minimal fix:** Per-repo asyncio lock around the whole sequence; verify `rev-parse --abbrev-ref HEAD == merge_target` immediately before merging.
- **Confidence:** high

### [IMPORTANT] `sync_worktree` "touch" is a no-op — synced worktrees still go stale
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_sync.py:133`
- **Failure mode:** `worktree_storage.update(worktree_id)` with no fields short-circuits (`if not fields: return`), so `updated_at` never refreshes; actively synced worktrees age into the cleanup Blocker's deletion path.
- **Minimal fix:** Update a real field (`last_synced_at`) or add a `touch()`.
- **Confidence:** high (no-op); med (intent)

### [IMPORTANT] `claim_worktree` is check-then-act; concurrent claims silently steal ownership
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_lifecycle.py:54-64`; `storage/worktrees.py:329-340`
- **Failure mode:** Read check then unconditional UPDATE; two concurrent claimants both succeed, last write wins — for the isolation primitive whose whole point is parallel agents.
- **Minimal fix:** Conditional UPDATE (`WHERE agent_session_id IS NULL OR = caller`), 0 rows = failure.
- **Confidence:** high

### [IMPORTANT] `delete_worktree` without a git manager silently orphans the git worktree and branch
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_lifecycle.py:142, 152, 173-175`
- **Failure mode:** `ctx.git_manager` None + no usable `project_path` → dirty check and git deletion skipped, DB record still deleted, `success: True` returned. On-disk worktree/branch remain, untracked, and `.git/worktrees` registration blocks branch re-creation.
- **Minimal fix:** Fail (or require an explicit flag) when the worktree exists on disk and no git manager could be resolved.
- **Confidence:** high

### [IMPORTANT] `delete_worktree` `force` silently escalates to force-deleting the unmerged branch
- **Where:** `src/gobby/mcp_proxy/tools/worktrees/_lifecycle.py:114, 153-158`; `worktrees/git/_lifecycle.py:184-187`
- **Failure mode:** `force` is documented as working-tree-only ("even if there are uncommitted changes") but is forwarded into branch deletion, selecting `-D`. Forcing past a dirty `.mypy_cache` also destroys unmerged commits.
- **Minimal fix:** Decouple: always `-d`; separate `force_delete_branch` flag for `-D`.
- **Confidence:** high

### [IMPORTANT] `get_handoff_context` silently falls back to an arbitrary session's handoff (cross-project)
- **Where:** `src/gobby/mcp_proxy/tools/sessions/_handoff.py:230-243` (options 2-3), link at `:271-274`
- **Failure mode:** If an explicit `session_id` resolves but yields no handoff, the code falls through to `list(status="handoff_ready", limit=1)` — unscoped by project/machine/source — and returns that session's summary as if requested; with `link_child_session_id` it also reparents the child onto the unrelated session.
- **Why it matters:** Agents resume from another project's context; lineage corrupted; reported as success. (File had uncommitted edits at review time — re-verify before fixing.)
- **Minimal fix:** Never fall through when an explicit `session_id` was provided; scope option 3 by current project.
- **Confidence:** high

### [IMPORTANT] `set_handoff_context` advertises a REQUIRED `session_id` parameter that does not exist
- **Where:** `src/gobby/mcp_proxy/tools/sessions/_handoff.py:83` (description) vs `:86-94` (signature)
- **Failure mode:** Description says "session_id: (REQUIRED)"; the function takes no such param and always acts on `get_current_session_id()`. `_prepare_call` silently drops the arg — handoff content written to the wrong session with no error.
- **Minimal fix:** Remove the description line or add and resolve the parameter; separately, make `_prepare_call` error on unknown args.
- **Confidence:** high

### [IMPORTANT] `record_verification_evidence` read-modify-write race loses evidence
- **Where:** `src/gobby/mcp_proxy/tools/sessions/_verification.py:102-116`
- **Failure mode:** `get_variables` → append in Python → `merge_variables` is not atomic; concurrent recordings (hook + tool) overwrite each other's appends. Evidence feeds completion-readiness gating.
- **Minimal fix:** Atomic jsonb array append in one transaction, or a per-session lock.
- **Confidence:** med

### [IMPORTANT] `search_skills` turns a session-lookup failure into an empty allowlist → silently zero results
- **Where:** `src/gobby/mcp_proxy/tools/skills/search_skills.py:111-115` (filter applied `:124`)
- **Failure mode:** On any exception from `get_active_skill_names`, `active_names = []` — a non-None empty allowlist rejects every skill (`skills/search.py:440-441`); `success: True, count: 0`. `list_skills.py:52-55` handles the same failure correctly (filter unset).
- **Minimal fix:** `active_names = None` on exception, matching list_skills.
- **Confidence:** high

### [IMPORTANT] `install_skill` security scan covers only SKILL.md; bundled files persist unscanned
- **Where:** `src/gobby/mcp_proxy/tools/skills/install_skill.py:191-194` (scan) vs `:237-252` (persistence)
- **Failure mode:** `scan_skill_content` runs on the main body only; `parsed_skill.loaded_files` (references/, scripts/ from GitHub/ZIP/hub) are stored verbatim and served via `get_skill_file`. A skill passes the scan with a benign SKILL.md and delivers its payload in an auxiliary file.
- **Minimal fix:** Scan each text loaded_file before `create_skill`/`set_skill_files`.
- **Confidence:** high (gap); med (exploitability)

### [IMPORTANT] GitHub install path traversal: URL `path` segment never validated
- **Where:** exposure `src/gobby/mcp_proxy/tools/skills/install_skill.py:146`; root cause `skills/loader.py:317-354` (`_validate_github_ref` skips `ref.path`), `:841-844` (`repo_path / ref.path`)
- **Failure mode:** `install_skill("https://github.com/owner/repo/tree/main/../../../../some/dir")` — the explicit-URL branch bypasses install_skill's own `..` check (which guards only the implicit owner/repo pattern, `:118-123`). After clone, the join escapes the skill cache; a leading `/` joins as absolute. Any local directory with a SKILL.md can be persisted as an installed skill.
- **Minimal fix:** Reject `..`/leading-`/` in `ref.path`; resolve and assert `relative_to(repo_path.resolve())` (the guard `extract_zip` already uses).
- **Confidence:** med

### [IMPORTANT] `send_keys` forwards arbitrary keystrokes to another session's terminal without ownership checks
- **Where:** `src/gobby/mcp_proxy/tools/sessions/_terminal.py:508-525` (resolution shared with `compact_self`/`capture_output`, `:174-220`)
- **Failure mode:** Any session resolvable by `#N`/prefix can be driven — type commands, approve trust prompts — with no caller-ownership or project check. (File had uncommitted edits at review time — re-verify before fixing.)
- **Minimal fix:** Restrict targets to the caller's own session or same-project/agent-tree sessions.
- **Confidence:** med (intended scope may be operator-only; no scoping exists in code)

### [IMPORTANT] `capture_baseline_dirty_files_tool` runs blocking git on the event loop
- **Where:** `src/gobby/mcp_proxy/tools/sessions/_actions.py:46-59` → `workflows/git_utils.py:202-214`
- **Failure mode:** `async def` calls sync `get_dirty_files` (blocking `subprocess.run`) directly; large/slow repos stall the loop.
- **Minimal fix:** `await asyncio.to_thread(get_dirty_files, project_path)`.
- **Confidence:** high

- **[NIT] `_porcelain_pathspec` has a confusing final branch** — `worktrees/_sync.py:22-27`; 3-char lines return whole line; works only because porcelain always has `XY ` prefix.
- **[NIT] `compact_self` web_chat branch can compact the wrong session id** — `sessions/_terminal.py:575-580`; subtle asymmetry between resolved/unresolved refs; undocumented.
- **[NIT] `list_skills` over-fetch can still under-deliver** — `skills/list_skills.py:95-110`; bounded over-fetch can return short of `limit` while more matches exist; silent truncation.
- **[NIT] `generate_worktree_path` can collide across distinct branches** — `worktrees/_helpers.py:55-58`; `feature/x` and `feature-x` sanitize identically; create guards on branch name, not path.

## Systemic patterns

1. **Check-then-act without atomicity, everywhere ownership matters.** `claim_task`, `claim_clone`, `claim_worktree`, spawn dedup, `merge_start` resolution adoption, `resume_pipeline`, `record_verification_evidence`, workflow-instance `set_variable` — all read state, await, then write unconditionally. The pattern currently "works" because the event loop serializes most callers, which is exactly the property the next fix (offloading blocking I/O) removes. Fix the claims as conditional UPDATEs and the resume/cancel transitions as compare-and-swap *before or with* the async offload work, or the offload will surface these races at once.

2. **Sync blocking I/O on the daemon event loop.** `InternalToolRegistry.call` (`internal.py:281-284`) invokes sync tool funcs inline, and many async handlers call sync git/subprocess directly (`clones.py` wholesale, `agents.py` tmux kill, tasks delivery/affected-files, sessions baseline capture, `task_github.py` gh). One dispatcher-level `asyncio.to_thread` offload plus a sweep of inline-sync-in-async-def closes the class. `merge.py` and `hub.py` already model the correct patterns.

3. **Destructive git operations conflate "force remove working tree" with "force delete branch."** A single `force=True, delete_branch=True` flows from cleanup loops, create-failure cleanup, and `delete_worktree` straight into `git branch -D` + `rmtree`, with dirty/merged checks present in one path and absent in the adjacent ones. Root of both worktree Blockers and several Importants.

4. **Silent fallback / success-without-effect.** Tools repeatedly report success when nothing happened or the wrong thing happened: invalid cron schedules persist dormant, `show_file` with no broadcaster, `unlink_identity` on unknown IDs, `import_workflow` writing files the runtime never reads, disabled pipelines running, handoff fallback to an arbitrary session, task/project resolution failures falling back to wrong scopes. Validation lives too deep (or nowhere) relative to where success is reported.

5. **Schema/description drift + silent unknown-arg dropping = wrong-data writes.** `_prepare_call` discarding unknown arguments converts documentation drift (`link_task_to_session` session_id, `set_handoff_context` session_id) into silent writes against the wrong entity instead of validation errors. Making the registry reject unknown args would turn this whole class into loud failures.

6. **Asymmetric terminal-state plumbing in agents.** Success/cancelled/error transitions live in three places with different guarantees for completion notification, claim recovery, and `terminal_reason`; the error path consistently loses. A single terminalization entry point keyed by status would remove the drift.

7. **Two parallel CRUD surfaces with unequal validation.** Type-specific Pydantic validation on `create_rule`/`update_rule`/`create_agent_definition` is bypassable via generic `create_workflow`/`update_workflow`; read-path scoping (memory get) not applied on write paths (memory delete/update); secret masking name-based while encryption is flag-based.

8. **Doc-vs-reality drift in CLAUDE.md's DB contract.** CLAUDE.md mandates `$N` placeholders, but the entire storage layer and these tools uniformly use psycopg `%s` (verified — zero `$N` in `src/gobby/storage/`). The code is internally consistent; the documented contract is stale and will mislead contributors. Fix the doc, not the code.

9. **Inconsistent error envelopes.** `success` vs `ok` vs raw raises (profiles, stage registry, plans, clones task-ref paths) — agents and pipelines branching on error shape get different contracts per tool.
