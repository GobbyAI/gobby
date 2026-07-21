# Review: build (gobby build service)

- **Scope:** `src/gobby/build/` — shared core (`service.py` shim, `coordinator.py`,
  `controls.py`, `lifecycle.py`, `results.py`, `options.py`, `validation.py`,
  `input_resolution.py`), workspaces (`workspaces.py`, `workspace_services.py`,
  `workspace_git.py`, `workspace_recovery.py`, `target_branch.py`, `branch_cleanup.py`,
  `delivery.py`), lifecycle/dispatch/recovery (`lifecycle*.py`, `task_lifecycle.py`,
  `plan_lifecycle.py`, `resume_lifecycle.py`, `claim_recovery.py`, `dispatch_tick.py`,
  `stage_manifest.py`, `runtime_hooks.py`, `project_*.py`), observability/profiles/artifacts
  (`observability.py`, `control_artifacts.py`, `profiles.py`). Plus the **three surfaces** that
  must route through the shared service: `cli/build.py`, `mcp_proxy/tools/build.py`,
  `servers/routes/build.py`. Cross-seam reads into `dispatch/`, `storage/tasks/`,
  `agents/isolation.py`, `worktrees/`/`clones/`, and tests.
- **Reviewer:** Claude Fable 5 — 4-agent parallel fan-out, all Blockers synthesizer-verified
  (the cross-project resolution traced through `resolve_task_reference`/`get_task` source).
- **Commit / branch:** `0.5.0` @ HEAD `2f76fc39f` (working tree clean at review time).
- **Summary:** 3 Blocker · 18 Important · 6 Nit — the three surfaces genuinely converge on the
  shared `build()`/`build_*_target` core (no surface reimplements the lifecycle), but a UUID
  task ref reaches the control path unscoped (cross-project destructive control), the agent
  cap is admitted with no global lock across the build/HTTP/CLI dispatch kicks, and
  `explain_dispatch` re-implements the dispatcher's gate logic and diverges from it. The
  workspace layer's dirty-tree and atomic-pair protections are mostly sound; the gaps are
  scoping, concurrency admission, and surface/option parity.

## Findings

### [BLOCKER] Build control actions (stop/resume/clean/restart) resolve a bare task UUID across project boundaries
- **Where:** `build/controls.py:408-416` (`_resolve_task_ref` → `task_manager.resolve_task_reference(input_ref, project_id)` then `get_task(resolved_id, project_id=project_id)`), reached by `build_stop_target:109`, `build_resume_target:148`, `build_clean_target`, `build_restart_target`. **Verified in source:** `storage/tasks/_id.py` `resolve_task_reference` UUID branch runs `SELECT id FROM tasks WHERE id = %s` (no `project_id`); `storage/tasks/_read.py` `get_task` runs `SELECT * FROM tasks WHERE id = %s` (project_id only used for the seq_num branch) — so passing `project_id` is a **no-op on the UUID path**.
- **Failure mode:** `gobby build clean <other-project-task-uuid> --force --yes` resolves the foreign task, `_affected_tasks` walks ITS subtree, `collect_clean_artifacts`/`delete_artifacts` (`control_artifacts.py:138,242-282`) remove those worktrees/clones from disk and clear their DB references, and `restart` resets their stage manifests — all under the *caller's* project. The control functions never assert `root.project_id == project_id`. History is recorded under the caller's project, hiding the cross-project effect. (The *start* path is unaffected — `input_resolution.looks_like_task_ref` only treats `#N`/digits as task refs, so a UUID falls through to plan-file resolution.)
- **Why it matters:** A destructive build action executes against another project's tasks — the project authorization boundary is bypassed for any UUID ref.
- **Minimal fix:** After `get_task`, assert `task.project_id == project_id` and raise `ValueError(f"task ref not found: {input_ref}")` otherwise; or scope the UUID branch of `resolve_task_reference`/`get_task` by project_id.
- **Confidence:** high (traced through source).

### [BLOCKER] Concurrent `run_heartbeat` invocations overshoot the global agent cap — no admission lock across build/HTTP/CLI dispatch kicks
- **Where:** `dispatch/dispatcher.py:177` (`count_active_agents` re-read per candidate, a plain `COUNT(*)`), with no global/advisory dispatch lock (verified: no `pg_advisory*` in `dispatch/`/`build/`/`system_automation.py`); concurrent ingress: `build/dispatch_tick.py:182` (`kick_dispatcher_tick`) called **directly** by HTTP `servers/routes/build.py:376` (bypassing `system_automation._tick_lock`) and by CLI `cli/build.py:454` in a **separate process** (`asyncio.run`, so the in-process lock can't serialize it), plus the interval tick and `_run_scheduled_project_dispatch` (which never acquires `_tick_lock`).
- **Failure mode:** The per-task `RuntimeDispatchMutex` serializes the *same* task but provides zero exclusion across *different* tasks. With cap=10 and 9 active, two concurrent heartbeats each read 9 (<10), each select a different ready task, each spawn → 11 active. The cap is a per-iteration read-modify-act with no atomicity around the spawn. (Same family as the workflows-engine pipeline-heartbeat finding, but independently reachable through the build surfaces.)
- **Why it matters:** `max_active_agents` is the documented hard cap; overshoot means resource exhaustion (extra worktrees/clones, LLM spend) exactly under the load it exists to bound.
- **Minimal fix:** Serialize cap admission behind a single shared async/DB advisory lock, or reserve the slot in the same transaction that creates the `agent_run` and re-check the count under that lock; route the HTTP/CLI kick through the same admission gate.
- **Confidence:** high (unlocked count-check + concurrent reachability verified).

### [BLOCKER] `explain_dispatch` reports an epic-gated root as ineligible with no action, while the real dispatcher fires an audit-marker action
- **Where:** `build/observability.py:79-140` (`explain_dispatch`) + `:501-536` (`_dispatch_block_reason` short-circuits on `epic_descendant_gate`); real path: `dispatch/dispatcher.py:130-270` (only the *ancestor* gate hard-skips), `storage/tasks/_automation.py` (`list_automation_candidates` uses the epic gate only to **sort** gate-present tasks last — they remain candidates), `dispatch/rules.py` (`epic_descendant_gate_rule` emits an `AppendAuditMarkerAction` for stage `epic_qa`/`ready`).
- **Failure mode:** `_dispatch_block_reason` treats the epic gate as a hard block → `explain_dispatch` returns `eligible=False, proposed_action=None`. But the dispatcher doesn't hard-skip the epic gate; the gated root reaches `dispatch_rules.evaluate`, which appends an epic-gate audit marker to the task description. `explain_dispatch` — the operator's "what will the dispatcher do" oracle — asserts nothing will happen while the dispatcher actually performs a write. The test `test_observability.py:481-518` codifies the wrong behavior. (The diverging action is an idempotent audit marker, not a destructive op — but per the stated bar, explain_dispatch diverging from actual dispatch in both `eligible` and `proposed_action` is the Blocker line.)
- **Minimal fix:** Don't treat `epic_descendant_gate` as a hard block in `_dispatch_block_reason`; mirror the dispatcher (only ancestor gate / mutex / claimed / automation / closed / escalated / dep-block / non-dispatchable-stage / cap are hard blocks) and let `dispatch_rules.evaluate` compute the real proposed action. Surface the gate as informational context.
- **Confidence:** high (verified the candidate query doesn't exclude gate-present tasks and the rule emits an action).

### [IMPORTANT] MCP surface exposes no stop/resume/clean/restart and omits dry_run/profile/cwd/unattended options
- **Where:** `mcp_proxy/tools/build.py` (only `build_task` registered; param list `:49-66` omits `dry_run`, `profile`, `cwd`, `clones_dir`, `unattended`, `planning_seed_state`) vs CLI/HTTP which expose all five lifecycle actions and all options.
- **Failure mode:** An MCP-driven agent cannot stop/clean/restart a build, cannot preview with `dry_run`, and cannot select a build profile — `BuildOptions` defaults (`profile="default"`, `dry_run=False`) are silently used. The contract says all three surfaces route control through the shared service; for control actions MCP has no surface, and for `build_task` it drops options the shared `build()` honors. (If intentional — agents shouldn't run destructive controls — it should be documented; the missing path can't *misbehave*, so this is below Blocker.)
- **Minimal fix:** Add the control tools and the missing options to the MCP surface, or document the deliberate omission.

### [IMPORTANT] `delivery_mode`/`delivery_target_repo` have no explicit-override path, and CLI/MCP can't select a non-default profile
- **Where:** `build/profiles.py:40-41` (delivery fields taken unconditionally from the resolved profile, unlike isolation/unattended/skip_stages which honor explicit markers); CLI `cli/build.py:129-175` and MCP `mcp_proxy/tools/build.py:49+` have no `profile` param (only HTTP does, `routes/build.py:50`).
- **Failure mode:** The one bundled profile with `delivery_mode: pull_request` (`build_profiles.yaml:49`) is unreachable from CLI/MCP, and no surface can override delivery mode against a profile default. Contract drift vs CLAUDE.md ("explicit CLI/MCP/HTTP fields override profile defaults; the default profile resolves unless a caller supplies another"). This is the "explicit choice can't be expressed" half of the precedence concern.
- **Minimal fix:** Add `--profile`/`profile` to CLI and MCP (thread `profile`/`profile_explicit` into `BuildOptions`); add per-field delivery overrides with explicit markers if intended.

### [IMPORTANT] `_root_build_state` reports "completed" for any closed root — masking failed/cancelled/escalated-then-closed builds
- **Where:** `build/observability.py:237-244` (`if root.closed_at is not None: return "completed"` — `closed_reason` never consulted); compounded by `Task.__post_init__` forcing `is_escalated=False` once closed (`storage/tasks/_models.py:228`), so `escalated_tasks` also reads 0 for an escalated-then-closed root.
- **Failure mode:** A root closed as `wont_do`/cancelled/failed reports `state="completed"` — identical to a clean finish, with no failure signal in the summary.
- **Minimal fix:** Consult `root.closed_reason`/`closed_commit_sha` and return `failed`/`cancelled` for non-success closes.

### [IMPORTANT] `gobby build clean --force` bypasses the dirty-worktree deferral the automatic merge-cleanup path applies — potential loss of uncommitted work
- **Where:** `build/controls.py:177-249` (`build_clean_target` calls `delete_artifacts(force=force)` with no `classify_dirty_descendant_worktree_artifacts`) vs `cleanup_successful_merge_artifacts:252-285` (runs `defer_active_agent_artifacts` + `classify_dirty_descendant_worktree_artifacts` first, deferring dirty worktrees unless proven integrated).
- **Failure mode:** Without `--force`, git refuses to remove a dirty worktree (work protected by accident). With `--force`, `delete_worktree(force=True)` hard-removes dirty worktrees regardless of integration state — `gobby build clean <epic> --force --yes` to clear a stuck build can silently destroy a descendant's uncommitted/unintegrated changes.
- **Minimal fix:** Run `classify_dirty_descendant_worktree_artifacts` in `build_clean_target` before `delete_artifacts` (deferred artifacts are already skipped); gate the dirty-override behind a separate explicit flag rather than the general `force`.

### [IMPORTANT] `branch_cleanup.project_path` falls back to `Path.cwd()` when project repo_path is null — force-deletes branches in the wrong repo
- **Where:** `build/branch_cleanup.py:140-144` (`project_path` returns `Path.cwd()` when `project.repo_path` is null — legitimately `str | None`, `storage/projects.py:63`), reached from `delete_orphan_build_branches:23` and `build_branch_candidates:60`; sibling `workspaces._project_repo_path:184-191` *raises* on missing repo_path.
- **Failure mode:** With a null repo_path, `git branch -D <candidate>` runs against whatever repo sits at the daemon's cwd; gobby-namespaced candidate names (`gobby/integration/<seq>-<slug>`, `task-<seq>-*`) colliding with branches there get force-deleted.
- **Minimal fix:** Raise (or no-op with an error) when repo_path is falsy, mirroring `_project_repo_path`; never fall back to cwd for destructive branch ops.

### [IMPORTANT] Blocking git subprocesses on the event loop — spawn-time workspace setup and clean/restart routes
- **Where:** spawn path: `build/workspaces.py:28,115` (`ensure_*_integration_workspace*` → `workspace_services._ensure_worktree/_ensure_clone` → `workspace_git` sync `subprocess.run`) invoked synchronously inside the async `spawn_agent` via `dispatch/spawn.py:154→350` and awaited at `dispatch/dispatcher.py:350` with no `to_thread`; clean/restart routes: `servers/routes/build.py:402,422` (`await build_clean_target/restart` directly) vs `:449,467,486` (read handlers correctly `await asyncio.to_thread(...)`). The correct pattern exists at `dispatch/workspace_merge.execute_merge_workspace:67`.
- **Failure mode:** A single epic spawn can run worktree/clone create + an `--ff-only` merge (60s) + per-closed-commit `--no-ff` merges (120s each) blocking the loop; a large-subtree clean/restart stalls the daemon's HTTP/WS/MCP servers for the duration.
- **Minimal fix:** Wrap the workspace-setup and clean/restart call sites in `asyncio.to_thread`, matching the read handlers and `execute_merge_workspace`.

### [IMPORTANT] `workspace_git._git` and `target_branch.py` skip `git_subprocess_env()` — git unresolvable under restricted-PATH launches
- **Where:** `build/workspace_git.py:190-206` (env from `os.environ` only) and `target_branch.py:43-87` (no PATH augmentation) vs `branch_cleanup.py:147-166` which calls `git_subprocess_env()` (`utils/git.py:30-41`, adds Homebrew/usr fallbacks when `git` isn't on PATH).
- **Failure mode:** Under a restricted-PATH daemon (packaged app / GUI launch), worktree/clone creation and integration merges fail with FileNotFoundError while `branch_cleanup` still works.
- **Minimal fix:** Route `workspace_git._git` and `target_branch` subprocesses through `git_subprocess_env()`.

### [IMPORTANT] Worktree/clone promotion mutates the DB role before the dirty-tree refresh check, and without an active-run guard
- **Where:** `build/workspace_services.py:163-168` (`_ensure_worktree`) and `:260-269` (`_ensure_clone`) — `update(... workspace_role="integration")` persisted *before* `_refresh_clean_git_dir` (which raises on a dirty tree); neither path checks `agent_runs` for an active run owning the worktree (unlike `recover_stale_integration_artifact:43` which calls `_active_workspace_run`).
- **Failure mode:** A dirty promoted worktree leaves the DB record half-promoted to `integration` with no successful refresh (state-corruption window, no data loss); and a worktree could be promoted+refreshed (merging `base_ref`, advancing HEAD) while an agent is still running in it.
- **Minimal fix:** Validate cleanliness before persisting the role change (revert on failure); add an `_active_workspace_run` check before promoting/refreshing.

### [IMPORTANT] `repair_expanded_epic_root_manifest_for_resume` deletes the stage manifest outside the dispatch mutex and non-atomically
- **Where:** `build/resume_lifecycle.py:298-303` (raw `db.execute("DELETE FROM task_stage_states WHERE task_id = %s")` outside any `task_dispatch_mutex`/transaction, then `initialize_manifest`). Every other stage-state writer takes the mutex via `StageStateMutexFactory`.
- **Failure mode:** A concurrent dispatcher heartbeat holding the mutex mid-action on this task isn't excluded from the DELETE; because the DELETE runs before re-init, `initialize_manifest` sees `existing == []` and skips its `ManifestAlreadyInitializedError`/shape-change guard (defeating the "clean/restart before shape changes" contract); a crash between DELETE and INSERT leaves the task with no manifest rows.
- **Minimal fix:** Do the DELETE+INSERT inside one `task_dispatch_mutex` lease and one transaction (a manifest-replacement method on `StageStateManifestOps`).

### [IMPORTANT] Resume reuses an existing worktree/clone at `development` without verifying no active run owns it
- **Where:** `build/resume_lifecycle.py:96,126-140` (`_resume_epic_workspace_refresh_required` returns False for `development` with valid integration artifacts → workspace reused as-is); `workspace_recovery._active_workspace_run:88-104` (the liveness check) is not invoked on this path; `recover_safe_build_claims` only releases `needs_review`/`review_approved` claims.
- **Failure mode:** Resume re-arms a workspace an orphaned agent process may still be mutating — two writers in one worktree, dirty/torn integration branch. (Matches the agents-review "resume reuses worktree_id without re-claiming.")
- **Minimal fix:** Call `_active_workspace_run` before reusing an integration/task workspace on resume; refuse/refresh if an active run references it.

### [IMPORTANT] Recovery claim-release and `task_recovery` mutex handling bypass the dispatch mutex
- **Where:** `build/claim_recovery.py:71` (`release_task_claim`) and `storage/tasks/_transitions.py:157` (bare `update_task`, no mutex); `agents/task_recovery.py:191-200` (the `lifecycle_stage != "in_progress"` branch releases the claim but **never** calls `_release_dispatch_mutex_for_run`, unlike the cancelled branch `:167` and failure branch `:202`); `build/resume_lifecycle.py:364` (cap UPDATE without the mutex).
- **Failure mode:** Releasing `claimed_by_session_id` instantly makes a task dispatch-eligible; these releases don't coordinate with the per-task dispatch mutex, so a release can land while the dispatcher is evaluating the same task (mitigated today by the dispatcher's `expected_stage` snapshot guard — defense-in-depth gap). The `task_recovery` asymmetry also leaves a dangling `task_dispatch_mutex` row until lease expiry (claim cleared, lease held).
- **Minimal fix:** Acquire the task mutex (or assert no live lease) before clearing the claim in recovery paths; release the run's dispatch mutex in the `:191` branch to match the other two.

### [IMPORTANT] HTTP control endpoints use inconsistent response envelopes; missing load-bearing tests for the dirty-guard and cap-overshoot
- **Where:** `servers/routes/build.py` — `post_build_resume` returns `_success_envelope` while `post_build`/`post_build_stop`/`post_build_clean`/`post_build_restart` return bare dicts (and error shapes differ: envelope JSON vs FastAPI `{detail}`); only `cli/_build_daemon.py:259-267` papers over both shapes. Tests: `tests/build/test_child_merge_repair.py` always returns empty `git status --porcelain`, so the dirty-refusal branch of `_refresh_clean_git_dir` (the load-bearing data-loss guard) is never exercised; no test asserts the cap holds under two concurrent heartbeats; no test for the manifest-DELETE atomicity.
- **Minimal fix:** Normalize the HTTP control envelopes; add tests for the dirty-refusal guard, concurrent-heartbeat cap, and manifest-replacement atomicity.

### [IMPORTANT] Sync DB I/O on the event loop in the main `build()` path; recovery `git status` on the dispatch hot path
- **Where:** `build/lifecycle.py` (`build()` is async but `_build_impl` does sync psycopg work — profile sync, recursive CTEs, artifact reads); HTTP `routes/build.py:335` and MCP `tools/build.py:95` `await build(...)` directly. `build/claim_recovery.py:47-85` runs up to N serial `git status` subprocesses (10s timeout each) at the top of every `kick_dispatcher_tick`/project dispatch.
- **Minimal fix:** Offload the sync sections via `to_thread`; bound/cache the recovery `git status` inspections off the hot path.

### [NIT] Build option/cleanup edge cases
- **Where:** `build/options.py:23` (`isolation_explicit` defaults `True` — a latent footgun, every other `*_explicit` defaults False; suppresses profile isolation for any direct construction that forgets to set it); `build/controls.py:153-154` (`build_resume_target` doesn't restore `unattended` that `build_stop_target` cleared — stop/resume not a faithful inverse); `build/branch_cleanup.py:71-72` (`task-<seq>-*` prefix can delete a user-created `task-5-experiment`); `build/workspaces.py:81-94` (stale opposite-family artifact pointer not cleared on integration write); `cli/build.py:208-212` (`--max-active-agents` lacks `IntRange(min=1)` unlike `--max-retries` — caught downstream by `_validate_max_active_agents`, so a ValueError not a clean Click error).

### [NIT] Dead/duplicated code
- **Where:** `build/observability.py:322-342` (`_count_active_agents` duplicates `dispatch/dispatcher.py:729` `count_active_agents` — drift risk for the explain cap math); `build/options.py:18`/`profiles.py:31` (`profile_explicit` threaded but never consumed in `build/`).

## Systemic patterns

1. **Two task-resolution paths with different project scoping.** The start path (`input_resolution`, `#N`/digits only, project-scoped) vs the control path (`resolve_task_reference`, also accepts UUIDs, UUID branch unscoped — and `get_task`'s direct-id path also unscoped). The control path is the weaker link and is the Blocker; the `_affected_tasks` subtree walk compounds it once a foreign root is resolved.
2. **No global dispatch admission control.** Cap enforcement is per-iteration per-heartbeat reads with no shared lock; multiple ingress points (interval loop, HTTP kick, CLI kick in a separate process, project-dispatch tasks) reach `run_heartbeat` with no coordination — the root of the cap-overshoot Blocker.
3. **The dispatch mutex protects the consumer (dispatcher) but not the writers.** Stage-state transitions and manifest init take the mutex; several claim/manifest/cap writes in `claim_recovery`/`resume_lifecycle`/`task_recovery` bypass it, leaning on the dispatcher's snapshot guard for safety instead of symmetric exclusion.
4. **`explain_dispatch` re-implements dispatcher truth** (the gate set and the active-agent count) instead of reusing `list_automation_candidates`/`count_active_agents`/`dispatch_rules.evaluate` — the root of the epic-gate Blocker and a latent drift source.
5. **Event-loop discipline is inconsistent within a file** — read handlers offload via `to_thread`; the heavier destructive control handlers and spawn-time workspace setup do not.
6. **Two clean paths with asymmetric safety** — automatic merge-cleanup defers dirty/unintegrated worktrees; the operator `--force` clean path does not.
7. **Surface fan-out is real but uneven** — all three converge on the shared core (no reimplemented lifecycle), but MCP lacks control tools and options, and HTTP envelopes are internally inconsistent.

## Verified non-bugs (cleared — don't re-chase)

- **The three surfaces genuinely converge on `build()`/`build_*_target`** — no surface reimplements the lifecycle logic, and validation runs once on the shared path for all three.
- **`max_active_agents` 0/negative does NOT pass the build-options path** — `_validate_max_active_agents` (`validation.py:51-53`) enforces `>= 1` on all surfaces; HTTP (`ge=1`) and MCP (`minimum:1`) add layer-1 guards. (Contradicts the config-review hypothesis for this path.)
- **Destructive clean/restart require explicit confirmation** (`yes=True`, raised otherwise); clean is blocked while automation/agents are live unless `--force`; dry-run is rolled back via `transaction_immediate` + `_DryRunRollback`.
- **`build()` error handling does not turn failures into successes** — exceptions propagate after `best_effort_finish_run(status="failed")`.
- **Atomic artifact pair writes hold** — worktree/clone/integration id+path+base_commit_sha go through `set_artifacts_atomic` → `_validate_constraints` in one transaction; `clear_isolation_pair` clears the whole triple together.
- **`delete_artifacts` doesn't strand state on failure** (sets `artifact.error`, keeps the pointer — no stale-pointer-to-live-worktree); `_remove_invalid_workspace_dir` only rmtree's a non-symlink non-git dir at the exact expected path.
- **`recover_stale_integration_artifact` refuses recovery when an active `pending`/`running` run owns the workspace**; `recover_safe_build_claims` only releases review-safe, clean, agent-free claims (excludes `in_progress`).
- **Profile resolution: a missing named profile raises** (`BuildProfileError`), not silent fallback; only an absent/None profile falls back to `default`. **Profile skip_stages do NOT reshape an existing manifest** (resume raises/warns; the bounded `{"pr"}`-on-expanded-epic exception is deliberate). Override precedence (isolation/unattended/skip_stages) is sound where wired.
- **History reads are bounded** (`_limit` cap 100; HTTP `Query(ge=1, le=100)`); current-stage selection is canonical "first not-done" everywhere (no off-by-one).
- **`delivery.py` deferred-PR boundary (#13552) is handled safely** — only persists a `pending` campaign row, no actual PR creation, no-ops for non-PR modes.
- **All git subprocesses pass an explicit `cwd`**; path-escape protection on plan files is tested; the cross-project coordinator session is explicitly guarded (`coordinator.py:42-50`).
- **`%s` placeholders are correct** per repo convention.
