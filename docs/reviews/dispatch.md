# Review: dispatch

- **Scope:** `src/gobby/dispatch/` — `dispatcher.py` (heartbeat scanner + action executor),
  `rules.py` (ordered lifecycle rules), `actions.py`, `write_set_guard.py`, `context.py`,
  `discovery_artifacts.py`, `spawn.py` (dispatch-side spawn + artifact prep),
  `workspace_merge.py` (merge at completion), `daemon_resume.py`, `prompts.py`, `mutex.py`,
  `lease_cleanup.py`, `audit.py`, `constants.py`. Cross-seam reads into `storage` (task_dispatch_mutex,
  integration_workspace_mutex), `system_automation.py`, `build/dispatch_tick.py`, `agents/`, and tests.
- **Reviewer:** Claude Fable 5 — 3-agent parallel fan-out, Blocker synthesizer-verified.
- **Commit / branch:** `0.5.0` @ HEAD `c5dbd3ecf` (working tree clean at review time).
- **Summary:** 1 Blocker · 9 Important · 6 Nit — the dispatcher's **determinism contract holds**
  (no LLM/clock/random decision; rules are pure functions of manifest state) and the
  optimistic-concurrency leasing is sound (advisory-lock acquire, holder-guarded release,
  guarded orphan sweep). The gaps: the documented *global* agent cap is enforced *per-project*
  (the structural root of the build review's cap Blocker), the heartbeat candidate loop has no
  `finally`/cancellation guard around the mutex, `epic_qa` review-exhaustion silently wedges
  the task, and the clone-backend merge can report success while never syncing the user's repo.

## Findings

### [BLOCKER] The global agent-slot cap is enforced per-project — concurrent project heartbeats overshoot N×cap
- **Where:** `dispatch/dispatcher.py:177` (`count_active_agents(resolved_db, project_id=project_id) >= cap`, verified) → `count_active_agents:729-752` (filters `parent_session.project_id = %s`); `system_automation.py:510-528` `_dispatch_projects` fans out `dispatch_project_once` over **every** project via `asyncio.gather`; cap `constants.py:5` `MAX_ACTIVE_AGENTS = 10`.
- **Failure mode:** The automation loop runs per-project heartbeats concurrently, and each counts only agent_runs whose parent session is in *its* project. With M active projects the global active-agent count reaches M×10 — there is no shared/global budget threaded through the gather (no semaphore/remaining-slot accounting). CLAUDE.md documents a *global* cap; the implementation makes it per-project. This is the structural root beneath the build review's "concurrent run_heartbeat overshoots the cap" Blocker (and is independent of the HTTP/CLI kick bypass — even a single process's own gather overshoots).
- **Why it matters:** Resource exhaustion (worktrees/clones, LLM spend, process pressure) exactly under load, with the documented hard cap silently violated.
- **Minimal fix:** Make the cap check global (count all pending/running agent_runs), or carry one shared remaining-slot budget across `_dispatch_projects`, decremented atomically per spawn. Keep per-project counting only as a secondary fairness limit.
- **Confidence:** high (verified).

### [IMPORTANT] Heartbeat candidate loop has no `finally`/cancellation guard — lease leak + orphaned agent on the 120s project-dispatch timeout
- **Where:** `dispatcher.py:205-267` (candidate `try`; `mutex.__enter__()` is a bare call, not `with`; except clauses at `:248/251/255` catch `(TypeError, AttributeError, psycopg.Error)`/`DispatchSpawnUnavailable`/`Exception` — none catch `BaseException`; **no `finally`**); cancellation from `system_automation.py:273` (`asyncio.wait_for(..., timeout=120s)`). The `execute_action` non-spawn paths DO have `finally: mutex.release()` (`:486-487`) — only the loop and the spawn-attach window are exposed.
- **Failure mode:** `CancelledError` is a `BaseException`; when the timeout fires while awaiting `_execute_action`, no except/finally runs and the acquired lease leaks. Worst window: cancel between `spawn_agent` returning a run_id and `mutex.attach(run_id)` (`:370-378`) — the agent_run exists (agent alive) but the lease is neither attached nor released and `_cleanup_unattached_spawned_run` never runs. The orphan sweep frees the un-attached lease after the 30s grace, and the next heartbeat re-dispatches the same task → double-spawn + orphaned agent process.
- **Minimal fix:** Wrap the candidate body in `try/.../finally` (or `with mutex:`) so cancellation always releases an un-attached lease; widen the spawn-attach cancellation guard to release + kill the just-created run if cancel lands after spawn but before attach.
- **Confidence:** med (orphan sweep self-heals the bare lease within 30s; the attach window and loop level are real exposures).

### [IMPORTANT] `epic_qa` review-round exhaustion silently wedges the task — no escalation
- **Where:** `rules.py:296-302` (`epic_qa_review_rule`: `if stage is None or _stage_review_exhausted(stage, context): return None` — verified) with `default_max_review_rounds=5` (`storage/tasks/_stage_registry.py:283`). No later rule matches `epic_qa`/`needs_review` (`epic_qa_advance_rule` only matches `review_approved`).
- **Failure mode:** After the epic reviewer fails to approve N rounds, the rule returns None and no other rule fires — the task produces no action and no escalation on every subsequent heartbeat (permanent wedge). Every sibling review rule escalates on exhaustion (`development_review_rule:269-270`, `expansion_review_rule:242-243`, `_spawn_on_stage:512-513` for planning/discovery); `epic_qa` is the only review stage that silently stalls. Untested.
- **Minimal fix:** Escalate on exhaustion (`EscalateAction(reason="epic_qa_max_review_rounds")`), mirroring development; add a test.
- **Confidence:** high.

### [IMPORTANT] Clone-backend merge can "complete" while the user's main repo branch is never synced — lost integration result on retry
- **Where:** `workspace_merge.py:75-147` (`_execute_merge_workspace_sync`), `:582-589` (`_sync_source_repo_branch` — `git fetch <clone> target:target` into `project.repo_path`), the `_is_ancestor` fast-path at `:98-101`.
- **Failure mode:** On a clone merge: create merge commit in the clone target → `_sync_source_repo_branch` → mark merged → complete. If the sync-back raises (the user has `target_branch` checked out so `git fetch` refuses, or a non-ff refspec is rejected — there's no `+`/`--force`), the stage fails, but the merge commit already exists in the clone. On the next retry, `_is_ancestor(clone_target, source_commit)` is True, so the fast-path completes the merge stage and **never re-runs the sync-back** — the merge "completes" but lives only in the clone; the user's real repo branch stays stale. Silent divergence between the recorded integration SHA and the user's repo.
- **Minimal fix:** Make `_sync_source_repo_branch` idempotent and re-run it on the `_is_ancestor` fast-path (or move it into `_complete_merge_stage`); make the fetch robust to a checked-out/non-ff target (surface as `needs_human`, not silent success). Add clone-backend merge tests (the entire merge suite is worktree-only — the repo-mutating clone path is untested).
- **Confidence:** med.

### [IMPORTANT] A transient `psycopg.Error` on one candidate aborts the entire heartbeat scan
- **Where:** `dispatcher.py:248-250` (`except (TypeError, AttributeError, psycopg.Error): mutex.release(); raise`).
- **Failure mode:** Any per-statement DB error (deadlock, serialization failure, lock/statement timeout — all plausibly transient and candidate-local) re-raises, unwinding the whole candidate loop; every later candidate is starved for that tick. Contrast the generic `except Exception` arm (`:255-266`), which logs, audits, releases, and *continues*. (Mutex is released before re-raise, so no leak — the harm is scan-abort.)
- **Minimal fix:** Treat `psycopg.Error` like the generic path (release, audit, `continue`); only abort on genuinely fatal connection-level errors.
- **Confidence:** high.

### [IMPORTANT] `_persist_spawn_artifacts` swallows DB errors — a successfully-spawned task can wedge with its workspace orphaned from artifacts
- **Where:** `dispatch/spawn.py:233,952-960` (logs and swallows `TaskArtifactConstraintError`/`ValueError`/`psycopg.Error` after `spawn_agent` succeeded); merge rule reads `worktree_id`/`clone_id` from artifacts (`rules.py:388-395`).
- **Failure mode:** The agent runs and commits work in the new worktree, but `task_artifacts` has no `worktree_id`, so the merge rule never emits a `MergeWorkspaceAction` — the completed work is stranded on disk, unreferenced, and the task stalls. Violates the atomic-pair-write intent at the dispatch seam.
- **Minimal fix:** Treat artifact-persist failure as a spawn failure (raise so the run is failed and the mutex/stage rolled back), or retry the atomic write before swallowing.
- **Confidence:** med.

### [IMPORTANT] Stale/interrupted in-progress merge (leftover MERGE_HEAD) produces a misleading conflict failure with no recovery sweep
- **Where:** `workspace_merge.py:90-101` (`_ensure_target_merge_safe` only blocks on dirty paths overlapping the incoming diff), `:612-643` (integration mutex, TTL 600s); no startup sweep for in-progress merges / expired `integration_workspace_mutex`.
- **Failure mode:** A daemon killed mid-merge leaves `MERGE_HEAD` in the target; after the lease expires a retry's `git merge` refuses ("MERGE_HEAD exists") → treated as a fresh `merge_conflict`, `_abort_merge` clears both, stage failed with a misleading reason. Self-heals only on the *second* retry.
- **Minimal fix:** Detect/abort an existing in-progress merge before merging (`git rev-parse -q --verify MERGE_HEAD`), or fail with a distinct `stale_merge_state` reason; add a startup sweep for expired integration leases.
- **Confidence:** med.

### [IMPORTANT] code+implementation_domain routing escalates when the domain agent is disabled, while docs/default deterministically fall back
- **Where:** `rules.py:765-771` (`_development_agent`: `category=="code"` with a valid `implementation_domain` returns `AGENT_BY_IMPLEMENTATION_DOMAIN[domain]` **without** `_agent_dispatchable`); `development_rule:259-262` then escalates `development_no_agent`. The docs path and the final default both chain to a dispatchable fallback.
- **Failure mode:** A `code/frontend` leaf with `frontend-developer` disabled escalates, while a `docs` leaf with `tech-writer` disabled falls back to `backend-developer` — inconsistent escalate-vs-fallback, contrary to the "missing-leaf-assignment falls back to backend-developer" determinism principle. Untested for the disabled-domain case. (Whether escalation is *intended* here is defensible — but the asymmetry is undocumented.)
- **Minimal fix:** Decide the contract: guard the domain branch with `_agent_dispatchable` (fall through to default), or document + test the deliberate escalation.
- **Confidence:** med.

### [IMPORTANT] `epic_descendant_gate` marker re-appends on every descendant state change — unbounded description growth
- **Where:** `rules.py:129-145` (`epic_descendant_gate_rule`), body builder `:799-812` (idempotency compares the *exact* marker text, whose body embeds each blocker's `stage=<name>:<state>`).
- **Failure mode:** As descendants advance, the body changes, the prior marker no longer matches, and a new marker is appended — one per distinct descendant-composition over the epic's life, bloating the task description (a load-bearing, parsed field). Deterministic (not a determinism bug) but unbounded growth + noisy audit.
- **Minimal fix:** Key idempotency on a stable identity (heading + sorted blocker task_ids), or replace-in-place the existing "Epic QA deferred" marker.
- **Confidence:** med-high.

### [NIT] Dead/latent code and timestamp hygiene
- **Where:** `dispatch/actions.py:77` (`AdvanceLifecycleAction` declared and in the `Action` union but no executor branch and no rule emitter — if ever emitted, `execute_action` raises `TypeError` → whole-scan abort); `rules.py:842-845` (`_has_isolation_pair` dead — zero callers, encodes the old pre-spawn isolation check the rules deliberately no longer do); `rules.py:321-324` (`pr_review_rule` redundant always-None branch); `rules.py:819-820` (`_is_leaf`'s `task.children` clause is vestigial on reloaded candidates — leaf/epic rests on `task_type`); `_dispatch_mutex.py` (mixed `_coerce_timestamp` ISO-string vs `CURRENT_TIMESTAMP` writes to `updated_at`, on which the orphan-sweep DELETE does an exact-string guard — self-consistent today, fragile); `lease_cleanup.py:13-32` (startup `sweep_expired_leases` SELECT-then-unconditional-`force_release` is a startup-only TOCTOU that could drop a concurrently-attached lease — guard on `run_id IS NULL`).

### [NIT] `failure_context` injected into spawn prompts is agent-authored audit text
- **Where:** `dispatch/prompts.py:44-51` consuming `context.failure_context` (from prior failure markers, `context.py:91,162`). Gobby-internal provenance, not external input; the prompt cannot set skills/rules/tools (those come only from `action.additional_skills`/`agent_body`). Second-order text-injection surface only; optionally length-cap.

## Systemic patterns

1. **The cap/concurrency model is project-sharded but documented as global.** Per-project `count_active_agents` + per-project `run_heartbeat` + `asyncio.gather` fan-out collectively defeat the single documented cap — the structural root of the build-review Blocker.
2. **Cancellation-safety is uneven.** Non-spawn action paths get `finally: mutex.release()`; the candidate loop (which manually `__enter__`s the mutex) and the spawn-attach step have no `BaseException`/`finally` coverage. The codebase intends cancel-safety (a dedicated test covers the spawn-*call* window) but missed the loop and attach windows.
3. **Per-stage exhaustion handling is inconsistent** — development/expansion/planning/discovery escalate on review-cap; `epic_qa` silently returns None and wedges. Routing all `needs_review`/`in_progress` exhaustion through one shared helper would prevent the drift.
4. **Idempotent-retry via `_is_ancestor` is the merge subsystem's safety net but also skips post-merge side effects** (the sync-back), which is the clone-merge data-divergence gap. Any post-merge side effect must be idempotent and replayed on the fast-path.
5. **Error-swallowing at write seams** (`_persist_spawn_artifacts`, merge cleanup) trades a hard failure for a silent stall; the dispatcher's stall behavior is worse than an explicit failure because there's no automatic re-derivation of the missing pointer.
6. **Worktree path is well-tested; the clone path (the only backend that writes the user's real repo) is not.**

## Verified non-bugs (cleared — don't re-chase)

- **The dispatcher determinism contract holds** — no LLM/expansion/network/clock/random decision in `dispatcher.py` or `rules.py`; pipeline input rendering uses `StepRenderer` over task context only; expansion/prompting happen inside spawned agents, not inline.
- **Per-task acquire is race-free** — `pg_advisory_xact_lock(hashtext('dispatch_mutex:<task_id>'))` inside `transaction_immediate` serializes the read-then-upsert; `release_mutex` is holder-guarded and idempotent; the orphan-sweep DELETE is guarded on exact `updated_at` so it can't free a lease a holder just refreshed.
- **Attached leases are not swept while a run is active** (sweep LEFT-JOINs agent_runs, excludes pending/running); the spawn-attach failure window terminalizes the created run (`_cleanup_unattached_spawned_run`, tested); the unattached-window crash is reclaimed by `sweep_orphan_no_run_dispatch_mutexes` after grace.
- **The epic-gate RULE side is correct** — the dispatcher does NOT hard-block on the epic descendant gate; it runs `evaluate()` and the rule appends a marker while the spawn rules decline. (The `explain_dispatch` divergence is an observability bug, filed in the build review — not a rules bug.) Rule ordering is first-match with `_AUTO_ADVANCE_DEDICATED_STAGES` deferral; no conflicting-action overlap found.
- **No force-release abuse** — all `force_release*` callers are legitimate (orphan sweep post-grace, terminal run events, operator build lifecycle).
- **Merge safety holds for the common cases** — `_local_target_path_if_checked_out` + `_ensure_branch` guard wrong-target merge; `_ensure_target_merge_safe` blocks overlapping dirty work; conflict abort preserves the target; auto-resolution is scoped to exactly `.gobby/project.json`/`docs/guides/README.md`; the sync merge runs via `asyncio.to_thread` (no loop block in the dispatch merge path).
- **Prompt structured-control is clean** — prompts carry only role/contract/title/ref/reason/failure_context; skills/rules/tools/model derive from `action`/`agent_body`, never from task content.
- **write_set_guard is a best-effort same-heartbeat overlap serializer over `task_affected_files`, not an isolation boundary** — an untracked task is by design, not a bypass; real isolation is the worktree/clone.
- **`%s` placeholders are correct** per repo convention.
