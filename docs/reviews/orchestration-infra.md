# Review: worktrees + clones + autonomous + scheduler + events

- **Scope:** `src/gobby/worktrees/` (git lifecycle + merge), `src/gobby/clones/`, `src/gobby/autonomous/`, `src/gobby/scheduler/`, `src/gobby/events/` — plus the cross-cutting consumers (`agents/isolation.py`, `mcp_proxy/tools/{merge,clones}.py`, `runner_lifecycle_*`, `workflows/pipeline_executor.py`, `sync/linear.py`).
- **Reviewer:** Claude Fable 5 (synthesizer over 6 parallel Fable 5 sub-reviewers; every Blocker re-verified against source by the synthesizer)
- **Commit / branch:** 52c7218af / 0.5.0
- **Summary:** 7 Blocker · 25 Important · 15 Nit — two real data-loss/availability defects (a clone delete that destroys non-git directories, a manual merge that commits conflict markers as success) sit alongside a cluster of latent-but-shipped failures: an autonomous-safety layer that is wired to nothing, a scheduler that records every non-raising failure as a successful run and no longer reclaims hung runs, and an event registry that is correct only by single-thread accident.

## #16724 current nit resolution

- Resolver coverage now exercises EOF conflicts, standalone `=======` content, diff3 `|||||||` rejection, intentional empty files, and exact repo-root `.gobby/*.jsonl` matching (`worktrees/merge/resolver.py`; `test_resolver_content_flow.py`; `test_resolver_implementation.py`).
- Dispatcher cap-only and stop-reason outcomes now persist failed cron runs with explicit errors (`scheduler/executor.py`; `test_dispatch_executor.py`).
- Remaining live worktree, clone, and event nits were fixed at their current source boundaries with focused tests. The prior `_locking.py`/`_merge.py` dead-code claims and pre-restoration cron-timeout wording were stale and removed from the nit ledger.

## Findings

### [BLOCKER] `delete_clone(force=False)` recursively deletes any non-git directory — the safety gate fails open

- **Where:** `src/gobby/clones/git.py:457-468` (delete guard) + `:526-546` (`get_clone_status` failure handling)
- **Failure mode:** `delete_clone` refuses a non-force delete only when `get_clone_status(...).has_uncommitted_changes` is True. `get_clone_status` runs `git status --porcelain` and populates its flags **only inside `if status_result.returncode == 0:`** (`:526`); when the path is not a git repo (or git errors), the block is skipped and it returns `CloneStatus(has_uncommitted_changes=False, ...)`. `delete_clone` reads that as "clean" and `shutil.rmtree`s the directory. The reviewer reproduced it live: `delete_clone(plain_dir, force=False)` returned `success=True` and destroyed a directory containing a file. There is no confinement check that the target is under a clone root or even a git repo.
- **Why it matters:** A single wrong/stale path from a drifted DB row (`clones.clone_path`, `task_artifacts.clone_path`) flowing through `build/control_artifacts.py:266` (`delete_artifacts`) or the MCP `delete_clone` tool triggers unconditional recursive deletion of arbitrary directories with the documented uncommitted-changes protection silently bypassed.
- **Minimal fix:** Have `get_clone_status` return `None` when the `git status` call fails instead of fabricating an all-False status; in `delete_clone`, when `force=False`, refuse deletion if status is `None`/`branch` and `commit` are both `None`; confine the resolved absolute path to `~/.gobby/clones/`.
- **Confidence:** high — verified the `returncode==0`-gated population and the rmtree-on-not-uncommitted path; reviewer reproduced live.

### [BLOCKER] `merge_apply` commits manually-resolved content with conflict markers as a successful merge

- **Where:** `src/gobby/mcp_proxy/tools/merge.py:503-508` (manual store, verbatim), `:665-674` (write + `git add` in `merge_apply`), `:684-693` (only check is `get_unmerged_files`), `:726-740` (commit); `resolved_content` is never marker-scanned
- **Failure mode:** `merge_resolve(conflict_id, resolved_content=...)` stores arbitrary text verbatim via `update_conflict` with no validation. `merge_apply` writes `conflict.resolved_content` to disk, `git add`s it, and the only gate is `get_unmerged_files` — which is `git diff --diff-filter=U`; once a file is staged, git no longer reports it unmerged regardless of content. So content still containing `<<<<<<<`/`=======`/`>>>>>>>` markers passes the only check and is committed. The AI tiers marker-check via `clean_ai_source_response` (`resolver.py:171-173`), but the manual path and the apply boundary have zero defense-in-depth.
- **Why it matters:** A partially-resolved file (markers left in) is committed as a "successful" merge — corrupted source in history, reported `success: True`.
- **Minimal fix:** Add a shared `assert_marker_free(content)` (reuse `_CONFLICT_MARKER_LINE_RE`) used both in `merge_resolve` when `resolved_content is not None` and in `merge_apply` before staging; reject with `success: False` if any marker line remains.
- **Confidence:** high — verified verbatim store, write+add, and the `--diff-filter=U`-only gate.

### [BLOCKER] Non-raising handler/pipeline outcome is recorded as a successful cron run — backoff is permanently defeated

- **Where:** `src/gobby/scheduler/executor.py:67-89` (any non-exception return → `status="completed", error=None`) + `src/gobby/scheduler/scheduler.py:203-211` (`completed` → `consecutive_failures=0`). Confirmed blast radius: `src/gobby/sync/linear.py` `linear_sync_handler` catches its own exception and `return f"Linear sync failed: {e}"` (a string); `executor.py:240` `_execute_pipeline` returns `f"Pipeline completed with status: {execution.status}"` unconditionally while `PipelineExecutor.execute` returns a `failed`/`cancelled` execution instead of raising. Same risk shape for `github_triage`, `memory/dream`, `wiki`, `state-dispatcher`, `pipeline_heartbeat` handlers.
- **Failure mode:** A cron action that fails every invocation without raising (degraded envelope, caught-and-returned error string, pipeline ending `status=failed`) is written to `cron_runs` as `status="completed", error=None`, and `consecutive_failures` resets to 0 each time.
- **Why it matters:** Backoff (`_get_backoff_seconds`) keys off `consecutive_failures > 0` and never engages; operators/UI see green "completed" runs while the job is 100% broken. Spans handler, pipeline, and dispatcher action types. (The missing-handler path — `executor.py:301-307`, raise → failed run — is the one part that is correct.)
- **Minimal fix:** (1) Raise typed errors at the action seams — `linear_sync_handler` re-raises (or raises on non-zero error count); `_execute_pipeline` raises when `execution.status` is not a success terminal. (2) Define a structured degraded-result contract the executor maps to `status="failed"`, rather than relying on every adapter's exception discipline.
- **Confidence:** high — verified the executor completed-on-no-raise and the scheduler failure-reset.

### [BLOCKER] Steady-state hung-run reclamation was removed; a hung cron run holds its slot until daemon restart

- **Where:** `src/gobby/scheduler/scheduler.py:121-185` (`_check_due_jobs` performs no stale-run sweep); commit `d67cdaa4e` ("stop cron timeout false failures") removed `fail_stale_running_runs(running_timeout_seconds)` from the dispatch path. `src/gobby/storage/cron.py:713` (`fail_stale_running_runs`) now has zero callers in `src/` (verified repo-wide: definition only). `running_timeout_seconds` (default 600) is consumed only by dead code (next finding).
- **Failure mode:** If a cron run hangs (a handler awaits forever, a pipeline step blocks, an awaited coroutine never returns — handler/pipeline/agent_spawn have no execution-time bound; only `_execute_shell` has its own `asyncio.wait_for`), its `cron_runs.status` stays `running` indefinitely. `count_running()` keeps counting it and `has_running_run(job.id)` keeps returning True. Only `_fail_orphaned_running_runs_on_startup` (next restart) clears it.
- **Why it matters:** One hung run permanently consumes a `max_concurrent_jobs` slot (default 5) and permanently blocks that specific job from ever running again; N hung runs starve the scheduler. `running_timeout_seconds` advertises behavior that no longer exists.
- **Minimal fix:** Restore a steady-state sweep in `_check_due_jobs` scoped to `started_at` (not `triggered_at`) older than `running_timeout_seconds` — `fail_stale_running_runs` already filters on `COALESCE(started_at, triggered_at, created_at)`; wire it back in. Bound handler/pipeline/agent execution with `asyncio.wait_for` so the run actually stops rather than being relabeled.
- **Confidence:** high — verified zero callers and the removing commit.

### [BLOCKER] The autonomous stuck/progress-detection layer is built but never fed or queried — a wedged agent is never detected

- **Where:** `src/gobby/autonomous/progress_tracker.py` (`record_event:127`, `record_tool_call:178`, `is_stagnant:322`), `stuck_detector.py` (`record_task_selection:118`, `is_stuck:305`); construction at `hooks/factory.py:433-435`, stored at `hooks/hook_manager.py:133-135`
- **Failure mode:** Repo-wide search (excluding `autonomous/` and tests) finds zero production callers of `ProgressTracker.record_tool_call`/`record_event`, `StuckDetector.record_task_selection`/`is_stuck` (verified: the `record_event` hits elsewhere are unrelated classes — build_history, skill_manager, mcp_proxy metrics, workflows engine). The objects are built and stored as `self._stuck_detector`, but `_stuck_detector` is never read again. The `loop_progress`/`task_selection_history` tables are never written outside tests, so every `is_stuck` returns `is_stuck=False`.
- **Why it matters:** This is the layer whose entire job is to catch a runaway/wedged autonomous agent. Nothing feeds it tool calls or task selections and nothing calls `is_stuck`, so a wedged agent (task loop, tool loop, 600s+ of no progress) is never flagged. All the heuristics below are dead in production.
- **Minimal fix:** Wire a recorder into the PostToolUse / task-selection hook path and call `is_stuck(session_id)` from the autonomous heartbeat / lifecycle monitor, acting on `suggested_action`. If the layer is intentionally inert, gate it behind an explicit flag or remove it rather than shipping it silently.
- **Confidence:** high — zero-caller claim verified by repo-wide search.

### [BLOCKER] `MergeResolver.resolve()` reports a failed merge as `GIT_AUTO` success when a conflicted file can't be parsed

- **Where:** `src/gobby/worktrees/merge/resolver.py:546-549` (`if hunks:` gate drops files `extract_conflict_hunks` can't parse), `:454-461` (empty-conflicts fall-through returns `success=True, tier=GIT_AUTO`); parser divergence between line-based `extract_conflict_hunks` (`startswith("=======")`) and `_CONFLICT_BLOCK_RE` (`:112`, anchored, trailing-newline-required)
- **Failure mode:** `_git_merge` appends a conflicted file to `conflicts` only when `extract_conflict_hunks` returns non-empty (`:547`). For a real conflicted file whose markers the line parser deems malformed (e.g. a body line that itself `startswith("=======")`, or a binary/undecodable file via `except Exception` at `:556`), it returns `[]` and the file is silently dropped. `resolve()` then sees `conflicts=[]`, skips tier 2, and returns `success=True, tier=GIT_AUTO` while the file is still unmerged and marker-laden on disk. Reviewer verified the divergence live (`extract_conflict_hunks` → 0, `_CONFLICT_BLOCK_RE` → 1).
- **Why it matters:** A merge with unresolved conflicts is reported as a clean success at the resolver API. **Mitigation (why latent):** the sole production caller `merge_start` independently re-derives conflicts via `collect_git_conflicts` (`git diff --diff-filter=U`) and forces `success=False` (`merge.py:387-392`), so the live MCP flow is backstopped — but `resolve()` is public/exported and any other caller inherits the false success.
- **Minimal fix:** In `_git_merge`, record every `--diff-filter=U` file even when `extract_conflict_hunks` returns `[]` (with empty hunks rather than dropped); make `resolve()` treat a non-zero git-merge with empty parsed-conflicts as failure/human-review, never `GIT_AUTO`.
- **Confidence:** high — verified the `if hunks:` gate and the `GIT_AUTO` fall-through.

### [BLOCKER] `merge_branch` loses the original checkout when the main repo is in detached HEAD

- **Where:** `src/gobby/worktrees/git/_merge.py:51-57` (capture) + `:157-167` (finally restore)
- **Failure mode:** `original_branch` is captured via `git rev-parse --abbrev-ref HEAD`, which returns the literal string `"HEAD"` in detached-HEAD state (verified empirically). The function runs `git checkout target_branch`, then the `finally` restores with `git checkout HEAD` — a no-op that leaves the repo on `target_branch` instead of the original detached commit. The original checkout is silently abandoned, with `success=True`. **Latent:** `_merge.merge_branch` is reachable only via the `manager.py:94-100` facade, which has no non-test callers (the live merge path is the inline implementation in `mcp_proxy/tools/worktrees/_sync.py`; the `merge_branch` callers in `mcp_proxy/tools/clones.py:526` hit `CloneGitManager`, a different class). Severity is on the latent bug, not active blast radius.
- **Why it matters:** If wired up (or if the clone-side twin at `clones/git.py:695-700` is exercised in a detached clone), a user/agent's checkout position is silently moved with no error.
- **Minimal fix:** When `original_branch == "HEAD"`, capture the SHA via `git rev-parse HEAD` and restore to that; or refuse to operate when detached. Either fix `_merge.py` and route `merge_worktree` through it, or remove the dead module.
- **Confidence:** high — detached-HEAD `abbrev-ref` behavior and the dead-code facade both verified.

### [IMPORTANT] `run_now` has no overlap guard — a manual trigger runs concurrently with a scheduled run of the same job

- **Where:** `src/gobby/scheduler/scheduler.py:273-296` (`run_now` calls `create_run` + `create_task` with no `has_running_run` check), reachable via MCP (`mcp_proxy/tools/cron.py:286`) and HTTP (`servers/routes/cron.py:203`); contrast `_check_due_jobs:142` which guards
- **Failure mode:** An operator/agent invokes `run_now` while a scheduled run is in-flight (or double-fires `run_now`), producing two concurrent executions of the same job. For non-idempotent actions (pipeline mutating tasks, shell writing files, agent_spawn) this risks duplicate side effects.
- **Minimal fix:** In `run_now`, `if self.storage.has_running_run(job_id): return None` before `create_run`; ideally make create+running-check atomic (see next).
- **Confidence:** high.

### [IMPORTANT] Cron overlap guard is check-then-act on a status the new run doesn't yet hold

- **Where:** `scheduler.py:142` (`has_running_run`) + `storage/cron.py:699-711` (checks `status='running'`) + `:607-643` (`create_run` inserts `status="pending"`, default at `cron_models.py:124`) + `executor.py:62-63` (status flips to `running` only once execution starts)
- **Failure mode:** Between `create_run` (pending) and `execute` (running), the run is invisible to both `has_running_run` and `count_running`. A heartbeat tick racing a `run_now` (or two dispatch paths) can both pass the guard; `count_running` undercounts pending runs, so `available_slots` can go over budget within a tick. The guard is advisory, not a per-job mutex (cron has no equivalent of the dispatcher's `task_dispatch_mutex`).
- **Minimal fix:** Have `create_run` insert as `running` (or a guarded conditional insert / unique partial index on `(cron_job_id) WHERE status IN ('pending','running')`); or count `pending` in the guards. The atomic-insert option is correct.
- **Confidence:** high.

### [IMPORTANT] Dispatch bookkeeping failure orphans a `pending` run and re-creates one every tick

- **Where:** `scheduler.py:165-184` — order is `create_run` → `compute_next_run` → `_update_job_bookkeeping(next_run_at=...)` → `create_task`; the outer `except Exception` at `:184` logs and continues
- **Failure mode:** If `_update_job_bookkeeping` raises (`SystemRowProtected`, transient DB error, `enabled=True requires next_run_at` validation), the already-created run is never dispatched and `next_run_at` is never advanced. The orphaned run stays `pending` forever (only `execute` transitions pending rows, and it never runs for it); because `next_run_at` wasn't advanced, the next heartbeat re-selects the same job and creates another pending run once per `check_interval_seconds`.
- **Minimal fix:** Advance `next_run_at` and create the run atomically; persist the run only after bookkeeping succeeds, or delete the run in the except branch.
- **Confidence:** high (mechanism), medium (frequency).

### [IMPORTANT] All scheduler DB access is synchronous on the asyncio event loop

- **Where:** `scheduler.py`/`executor.py` — `get_due_jobs`, `count_running`, `has_running_run`, `create_run`, `update_run`, `update_job`, `cleanup_old_runs` all call `HubDatabase.execute/fetchone/fetchall` (`def`, sync, per `storage/hub/postgres.py`)
- **Failure mode:** Each heartbeat blocks the event loop for the duration of these queries; under DB latency/lock contention the scheduler loop and every other coroutine stall. This is the established repo pattern (psycopg sync everywhere), so it may be intended architecture — flagged for the cross-cutting record, not as a scheduler-specific regression.
- **Confidence:** high (it is sync on the loop), low (that it is a regression vs intended design).

### [IMPORTANT] Dead code masks the removed hung-run sweep: `_planned_restart_source` / `_planned_restart_marker_max_age_seconds`

- **Where:** `scheduler.py:248-266` — `_planned_restart_marker_max_age_seconds` is called only by `_planned_restart_source`, which itself has zero callers in scheduler.py (the live `_planned_restart_source` callsite is in `utils/daemon_client.py`, a different class with its own copy). Stranded by commit `d67cdaa4e`.
- **Why it matters:** These are the sole remaining consumer of `config.running_timeout_seconds`, so a grep audit finds the field "used" and assumes the timeout is enforced — masking the removed-sweep Blocker.
- **Minimal fix:** Delete both methods (and re-evaluate `running_timeout_seconds`), or — tied to the sweep Blocker — repurpose the config back to the stale-run sweep it documents.
- **Confidence:** high.

### [IMPORTANT] `merge_branch` finally-restore failure is swallowed, leaving the main repo on the wrong branch

- **Where:** `src/gobby/worktrees/git/_merge.py:157-167`
- **Failure mode:** The `finally` restore runs `git checkout original_branch` inside a try/except that only logs on a raised exception; the checkout's **return code is never checked**. If the checkout fails (uncommitted state, non-zero exit without exception), the repo is left on `target_branch` and the caller still gets `success=True` with no signal.
- **Minimal fix:** Check `restore.returncode` and surface a warning in the result on failure.
- **Confidence:** high.

### [IMPORTANT] Partial-failure leaks the `worktree add -b` branch in every cleanup path that uses `delete_branch=False`

- **Where:** `agents/isolation.py:331-356` (`cleanup_environment` → `delete_worktree(force=True)` only), `build/control_artifacts.py:253`, `hooks/event_handlers/_misc.py:285`, `servers/routes/source_control.py:786`; root default at `worktrees/git/_lifecycle.py:150,229-243` (`delete_branch=False`)
- **Failure mode:** `create_worktree(create_branch=True)` creates a new branch via `git worktree add -b`. When setup fails after the worktree is created, `cleanup_environment` deletes the directory but passes `delete_branch=False`, so the freshly-created branch is orphaned. The MCP cleanup paths (`_cleanup.py:152,178`) correctly pass `delete_branch=True` — inconsistent semantics for the same operation.
- **Minimal fix:** Pass `delete_branch=True` (with `force=True` → `-D`) in `cleanup_environment` and the build artifact-deletion path when Gobby created the branch; or track and delete the created branch explicitly on rollback.
- **Confidence:** high.

### [IMPORTANT] `sync_from_main` non-conflict rebase/merge failure leaves the worktree mid-operation (no abort)

- **Where:** `src/gobby/worktrees/git/_lifecycle.py:264-359` (the post-CONFLICT branch returning "Failed to {strategy}" without `--abort`)
- **Failure mode:** Only failures whose output contains the literal substring `"CONFLICT"` trigger `git {strategy} --abort`. A rebase/merge that fails for other reasons after partially applying (unstaged changes, hook failure, TimeoutExpired mid-rebase) leaves `.git/rebase-merge`/`MERGE_HEAD` in place; the function returns `success=False` without cleanup, so the worktree is left inconsistent and the next sync's rebase misbehaves. The test `test_sync_rebase_failure_no_conflict` asserts only `success=False`, never that the operation was aborted.
- **Minimal fix:** On any non-zero sync result (not just CONFLICT), attempt `git {strategy} --abort` (ignoring its own failure) before returning; also abort on the TimeoutExpired path.
- **Confidence:** high.

### [IMPORTANT] `delete_worktree` reports `success=True` and leaks the branch when non-force `git branch -d` fails on unmerged commits

- **Where:** `src/gobby/worktrees/git/_lifecycle.py:229-243`
- **Failure mode:** With `delete_branch=True, force=False`, `git branch -d` refuses to delete a branch with unmerged commits (non-zero exit). The code returns `success=True` with a note that the branch wasn't deleted (and sets `error`), but worktree-removal succeeded. Callers checking only `result.success` treat it as clean and don't retry, leaving an orphaned branch that may hold the only copy of committed work.
- **Minimal fix:** Return `success=False` when branch deletion was requested but failed, or split the result so callers can distinguish worktree-removed-branch-leaked from fully-clean.
- **Confidence:** high.

### [IMPORTANT] `has_unpushed_commits` returns an inflated count and false-positive `True` when no remote-tracking branch exists

- **Where:** `src/gobby/worktrees/git/_branch.py:107-119`
- **Failure mode:** When `git rev-parse --verify origin/<branch>` fails (no upstream), the code counts `git rev-list --count <branch>` — the entire history reachable from the branch, not commits unique to it (verified: a branch one commit ahead of a 3-commit main reports count=4). For a fresh worktree branch identical to main it returns `(True, <full repo commit count>)`. Drives `use_local=True` in `agents/isolation.py:259-269`. Practical risk is bounded because `use_local=True` is the commit-preserving branch.
- **Minimal fix:** With no upstream, compare against the merge-base with the base branch (`git rev-list --count <base>..<branch>`), or document the count as "commits on branch."
- **Confidence:** high.

### [IMPORTANT] Blocking `git clone` (300–600s) runs on the asyncio event loop

- **Where:** `agents/isolation.py:489-494` (`create_clone` called directly in `async def prepare_environment`) → `clones/git.py:235-242`/`321-327` (`subprocess.run(..., timeout=300/600)`)
- **Failure mode:** `prepare_environment` is async and awaited on the event loop; it calls synchronous `create_clone`/`shallow_clone`/`full_clone` with no `asyncio.to_thread` wrapper, while neighboring blocking calls (`_capture_base_commit_sha`, `set_artifacts_atomic`) ARE wrapped. A slow/large clone stalls the entire daemon loop for minutes — heartbeat dispatch, WebSocket broadcasts, all concurrent HTTP/MCP handlers freeze.
- **Minimal fix:** `await asyncio.to_thread(self._clone_manager.create_clone, ...)`.
- **Confidence:** high.

### [IMPORTANT] No partial-directory cleanup when `git clone` exits non-zero

- **Where:** `clones/git.py:255-261` (shallow_clone) + `:340-346` (full_clone)
- **Failure mode:** On `returncode != 0` the methods return failure but, unlike the `TimeoutExpired`/`SubprocessError` branches, do NOT `shutil.rmtree(clone_path)`. Git self-cleans on most clean failures, but interrupted transfer / disk-full mid-checkout leaves a partial tree. Because both methods guard with `if clone_path.exists(): return failure`, a retry at the deterministic same path then fails permanently with "Path already exists."
- **Minimal fix:** In the non-zero-exit branch, `if clone_path.exists(): shutil.rmtree(clone_path, ignore_errors=True)` before returning, matching the exception branches.
- **Confidence:** medium.

### [IMPORTANT] Concurrent clone creation for the same branch is racy and collision-prone

- **Where:** `agents/isolation.py:592-596` (`_generate_clone_path`, deterministic `~/.gobby/clones/<project>/<safe_branch>`, no unique suffix) + `:430-498` (unlocked check-then-create)
- **Failure mode:** Two spawns for the same branch (or two tasks whose branch names sanitize to the same `safe_branch`, e.g. `feat/x` and `feat-x`) both miss the DB and target the same dir; the second `git clone` hits the `clone_path.exists()` guard and raises `RuntimeError("Failed to create clone")`, aborting that spawn.
- **Minimal fix:** Include a unique component (task ref / short uuid) in the generated path, and/or serialize clone creation per (project, branch) with a mutex.
- **Confidence:** medium.

### [IMPORTANT] MCP `delete_clone` rollback recreates the record with a new ID, orphaning references

- **Where:** `mcp_proxy/tools/clones.py:312-345` (deletes DB row first, then FS; on FS-delete failure calls `clone_storage.create(...)`)
- **Failure mode:** The tool deletes the DB record, then deletes files; if file deletion fails it "restores" via `clone_storage.create`, which mints a fresh `id`. The log says "Restored clone record for {clone_id}" but the row has a different primary key. Artifact/task pointers to the original `clone_id` are now dangling while the on-disk clone persists under a new id.
- **Minimal fix:** Delete files before the DB row, or restore by re-inserting the original `id`.
- **Confidence:** high.

### [IMPORTANT] Completion registry `register()` replace-on-collision orphans an in-flight `wait()` (lost wakeup)

- **Where:** `src/gobby/events/completion_registry.py:50-56` (register) + `:119-123` (wait); collision sites `agents/completion_subscribers.py:70-71`, `runner_lifecycle_subsystems.py:427-428`
- **Failure mode:** A coroutine blocked in `wait("c1")` holds `event_old = self._events["c1"]`. A second `register("c1", ...)` replaces the dict entry with `event_new`. A later `notify("c1")` sets `event_new` only; `event_old` is never set, so the waiter hangs until its `wait_for` timeout (or forever if `timeout=None`). Reviewer reproduced live (waiter timed out). Also discards the first registration's subscriber list. Recurrence of `docs/reviews/agents.md:208-211`.
- **Minimal fix:** In `register`, if `completion_id` exists, merge subscribers into the existing registration and reuse the existing Event; or have callers guard with `is_registered()` → `subscribe()`.
- **Confidence:** high (mechanism), medium (collision frequency today).

### [IMPORTANT] Completion registry `wait()` re-reads `self._results[id]` by key after waking — `KeyError` crash on a cleanup race

- **Where:** `src/gobby/events/completion_registry.py:119-123`; notify→cleanup-same-coroutine site at `runner_lifecycle_subsystems.py:421-442`
- **Failure mode:** `wait` does a fresh `self._results[completion_id]` lookup after `event.wait()` returns, not capturing the result at registration. If `notify` (sets result + event) then `cleanup` (`self._results.pop(id, None)`) run in one coroutine before the waiter resumes, `event.wait()` returns True but the result is gone → unhandled `KeyError` (reproduced live). Latent: the only notify→cleanup-same-coroutine site runs at restart against a fresh registry with no live waiter.
- **Minimal fix:** Snapshot the result before awaiting, or read `self._results.get(completion_id, {})` / raise a typed "result evicted" error instead of an unguarded subscript.
- **Confidence:** high (mechanism + repro), medium (reachability).

### [IMPORTANT] Completion registry has no lock; `notify` iterates the live subscriber list across `await`

- **Where:** `src/gobby/events/completion_registry.py` (no `asyncio.Lock` in the class); `notify` loop `:92-101` does `for session_id in self._subscribers.get(id, []): await self._wake_callback(...)`
- **Failure mode:** The loop iterates the live `self._subscribers[id]` and yields inside the body; a `subscribe(...)` appending during that await is included in the in-progress wake (confirmed live, benign under CPython). Correctness relies on an undocumented "loop-thread-confined, never mutate mid-notify" invariant; any future `asyncio.to_thread` mutation would corrupt the dicts silently.
- **Minimal fix:** Iterate a snapshot (`list(self._subscribers.get(id, []))`) and document the loop-thread confinement, or add an `asyncio.Lock` if off-loop mutation is ever needed.
- **Confidence:** high (mechanism), medium (benign today).

### [IMPORTANT] Autonomous count-based stagnation latch disables itself permanently after the first high-value event

- **Where:** `src/gobby/autonomous/progress_tracker.py:_check_stagnation` (~`:392-396`, `if high_value_events == 0 and low_value_events >= self.max_low_value_events`)
- **Failure mode:** The event-count stagnation rule requires `high_value_events == 0`. Once a session records a single high-value event (one `FILE_MODIFIED`), this branch can never fire again regardless of how many thousands of low-value events follow; only the time-based 600s rule remains.
- **Minimal fix:** Compare against high-value events since the last high-value timestamp (low-value accumulated since `last_high_value_at`), not total `== 0`.
- **Confidence:** high.

### [IMPORTANT] Autonomous unacknowledged stop signals leak forever — `acknowledge()` has zero callers, `cleanup_stale` only deletes acknowledged rows

- **Where:** `src/gobby/autonomous/stop_registry.py:acknowledge:155` and `cleanup_stale:244-269`
- **Failure mode:** No production caller of `StopRegistry.acknowledge` (verified). The only consumer, `has_stop_signal` (`safe_evaluator.py:549`), reads but never acknowledges. `cleanup_stale` deletes only `WHERE acknowledged_at IS NOT NULL`. So a signaled session's row stays `pending` indefinitely; `has_stop_signal(session_id)` keeps returning True forever for a reused/long-lived id, and a rule keyed on it keeps firing after the stop was honored. Session-end never clears it.
- **Minimal fix:** Call `acknowledge` when a session honors the stop (or clear on session termination), and broaden `cleanup_stale` to delete pending signals older than a max age.
- **Confidence:** high.

### [IMPORTANT] `signal_stop` pending-check is a non-atomic read-then-write across two pooled connections

- **Where:** `src/gobby/autonomous/stop_registry.py:signal_stop:77-105`
- **Failure mode:** `with self._lock:` serializes only within one process, but `get_signal()` and the `INSERT ... ON CONFLICT DO UPDATE ... acknowledged_at=NULL` run on two connections. Interleaving lets a just-acknowledged signal be silently resurrected (`acknowledged_at` reset to NULL) by a stale in-flight `signal_stop`. Benign today (single-process, ON CONFLICT keeps the row consistent), but fragile: once `acknowledge`/`clear` are wired, a racing `signal_stop` can un-acknowledge a stop.
- **Minimal fix:** Drop the pre-read; rely on one idempotent `INSERT ... ON CONFLICT DO UPDATE ... WHERE acknowledged_at IS NULL` (or `RETURNING` to detect new vs existing).
- **Confidence:** medium.

### [IMPORTANT] Autonomous tool-loop heuristic ignores argument values — false positives on varied calls

- **Where:** `stuck_detector.py:detect_tool_loop:277-279`; key source `progress_tracker.py:record_tool_call:226-228` (`"tool_args_keys": list((tool_args or {}).keys())`)
- **Failure mode:** The loop key is `f"{tool_name}:{tool_args_keys}"` — only the arg *names*, not values. Five distinct `Bash` calls (`pytest a`, `git status`, `ls`, …) share key `Bash:['command']`, hit `tool_loop_threshold=5`, and are flagged "stuck in tool loop" with `suggested_action=change_approach`. (Currently dead per the autonomous Blocker, but wrong if actuated.)
- **Minimal fix:** Include a hash/normalized form of the actual argument values in the loop key.
- **Confidence:** high.

### [IMPORTANT] Autonomous `record_tool_call` test/build classification is substring-based and defaults to high-value success

- **Where:** `progress_tracker.py:record_tool_call:208-220`
- **Failure mode:** (1) Test detection is `any(kw in command for kw in ["pytest","test","npm test","cargo test"])` — bare substring `"test"` matches `git checkout latest`, `requests`, etc. (2) The build branch `else: progress_type = BUILD_SUCCEEDED` marks any build-keyword command with no "error"/"failed" substring as high-value success, resetting the stagnation clock even if the build hung or produced no output. (3) Pass/fail uses case-sensitive `"FAILED"`/`"passed"` substring scans.
- **Why it matters:** Misclassifying failed/low-value work as `BUILD_SUCCEEDED`/`TEST_PASSED` resets `last_high_value_at`, masking genuine stagnation.
- **Minimal fix:** Tokenize the command (word boundaries / first token), require explicit success signals rather than defaulting to success, treat unknown/empty build output as low-value.
- **Confidence:** high.

## Nits

Re-triaged against current `0.5.0` for #16724; all surviving nit findings were fixed with focused regression coverage. Obsolete `_locking.py`/`_merge.py` dead-code claims and the pre-restoration cron-timeout wording were removed from this ledger.

## Systemic patterns

- **Errors-as-in-band-success at every action seam.** Some cron handlers still return error/degraded strings and pipelines return `status=failed` without raising. Structured action outcomes now cover mapping results and dispatcher stop reasons, but string-only adapters remain dependent on their own exception discipline. This is the same family flagged in `llm-prompts.md` — not subsystem-specific.
- **Operations marked done before they succeeded.** Cron runs → `completed`/`consecutive_failures=0` regardless of real outcome; `delete_worktree` → `success=True` on a failed `git branch -d`; `merge_branch` finally-restore with unchecked return code; pipeline run reported `completed` while `status=failed`.
- **Restore/abort/cleanup steps are best-effort with unchecked return codes and substring-gated triggers.** `sync_from_main` aborts only on the literal `"CONFLICT"` substring; the merge finally-restore ignores its exit code; clone non-zero-exit skips the rmtree the exception branches perform. Each leaves the system inconsistent while trending toward success.
- **Dead/unwired safety layers shipped as if active.** The autonomous stuck/progress-detection layer still has no recorder or `is_stuck` caller. Latent bugs accumulate in code that looks load-bearing.
- **Check-then-act guards without atomic DB exclusion.** Cron overlap (`has_running_run` on a status the new run doesn't hold yet; `run_now` bypasses it entirely) and `stop_registry.signal_stop` (read-then-write across pooled connections under an in-process lock that can't span connections). The dispatcher's `task_dispatch_mutex` is the pattern these subsystems lack.
- **Correct-by-accident concurrency.** The completion registry has no lock and relies on an undocumented single-loop-thread, never-mutate-mid-notify invariant; two of its three findings are latent bugs waiting on a future re-register or off-loop caller. The events lost-wakeup and `KeyError` races, and the autonomous count-latch and signal_stop race, are all "holds today, breaks on the next caller."
- **Heuristics standing in for semantic comparison.** Autonomous tool-loop keys on arg names rather than values; test/build classification uses bare substrings and defaults to high-value success. Each biases toward false negatives (looks productive) or false positives (varied work looks looped).
- **Subprocess hygiene is otherwise sound.** Across worktrees and clones, every git call is list-based (no shell → no injection from branch/path names) and time-bounded; credentials are redacted by modern git in the stderr that propagates. The blocking-clone-on-event-loop and the missing handler/pipeline/agent execution deadlines are the exceptions.
