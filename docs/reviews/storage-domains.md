# Review: storage domain CRUD

- **Scope:** `src/gobby/storage/` domain modules — `tasks/` (all 40 modules incl. stage/dispatch substrate), `sessions/` (all 17 modules), `agents/`, `skills/`, plus `memories.py`, `pipelines.py`, `mcp.py`, `cron.py`/`cron_models.py`, `worktrees.py`, `clones.py`, `merge_resolutions.py`, `expansion_runs.py`, `build_profiles.py`, `build_history.py`, `bin_update_state.py`, `communications.py`, `delivery.py`, `inter_session_messages.py`, `chat_messages.py`, `chat_attachments.py`, `github_triage.py`, `workflow_definitions.py`, `workflow_audit.py`, `plans.py`, `projects.py`, `prompts.py`, `session_models.py`, `session_lifecycle.py`, `session_resolution.py`, `session_tasks.py`, `token_events.py`, `model_costs.py`, `metric_snapshots.py`, `context_usage_snapshot.py`, `compaction.py`, `checkpoints.py`, `spans.py`, `task_affected_files.py`, `task_dependencies.py`, `auth.py`, `sql_dialect.py` — and the consumer seams (mcp_proxy tools, runner maintenance, gatekeeper) where row-selection semantics become side effects. Split boundary: hub/infra core (postgres.py, migrations, _ambient, executor, secrets, config_store) is `storage-core.md` (#15773).
- **Reviewer:** Claude (Fable 5) — 6 parallel subagent reviewers + synthesizer; all Blockers personally re-verified against source by the synthesizer.
- **Commit / branch:** ddce988e7 on `0.5.0`
- **Summary:** 6 Blocker · 67 Important · 44 Nit — the domain CRUD layer is broad and mostly defensive on reads, but write paths default to lock-less read-modify-write, check-then-act creates, and unguarded state transitions; the few correct implementations (seq allocation, github_triage claim, agent terminal transitions, worktrees.find_expired) prove the right patterns exist and simply weren't propagated to their twins.

## Findings

### [BLOCKER] Pipeline approval/resume tokens are multi-use and survive rejection — a spent or rejected token can resume a cancelled execution
- **Where:** `src/gobby/storage/pipelines.py:677-690` (`get_step_by_approval_token` — no status filter), `:640-642` (`update_step_execution` can set but never clear `approval_token`), `:140` (`resume_token = COALESCE(%s, resume_token)` — never clearable); consumers `src/gobby/workflows/pipeline/gatekeeper.py:162-179` (`approve_step`: token lookup → unconditional COMPLETED, no WAITING_APPROVAL check, token not cleared) and `:198-223` (`reject_step`: step FAILED + execution CANCELLED, token still resolves)
- **Failure mode:** (1) The same token approves repeatedly — each call re-marks the step COMPLETED and re-enters `PipelineExecutor` resume, re-running subsequent steps. (2) Approve-after-reject: reject cancels the execution but leaves the token resolvable; a later approve flips the failed step to COMPLETED and resumes the cancelled pipeline. (3) Concurrent approves both pass the check-then-act lookup and double-resume. (Synthesizer-verified all three storage facts and both gatekeeper paths.)
- **Why it matters:** Approval gates are the human checkpoint in front of dangerous pipeline steps; multi-use tokens that survive rejection defeat the gate and can run gated steps twice.
- **Minimal fix:** Atomic single-use consumption at the storage layer: `UPDATE step_executions SET status='completed', approval_token=NULL, ... WHERE approval_token = %s AND status = 'waiting_approval' RETURNING *`; same shape for reject; clear `resume_token` when an execution leaves WAITING_APPROVAL.
- **Confidence:** high

### [BLOCKER] Automatic clone reaper deletes clones regardless of status/ownership — `clones.find_expired` omits the `merged` filter its contract requires
- **Where:** `src/gobby/storage/clones.py:478-519` (no `status` filter, no `agent_session_id` guard; docstring claims "merge succeeded… Safe to delete"); `:377-392` (`record_sync` resets status to ACTIVE but never clears `cleanup_after`); fed by `src/gobby/mcp_proxy/tools/clones.py:574-575` (`merge_clone` sets `cleanup_after = now+7d` without setting status to merged); consumed hourly by `src/gobby/runner_maintenance.py:606-612` (`shutil.rmtree(path, ignore_errors=True)` then row delete)
- **Failure mode:** Any clone with an elapsed `cleanup_after` is rmtree'd — including an active, claimed clone an agent is still working in. Live path: `merge_clone` stamps a 7-day TTL while leaving status `active`; continued agent work (`record_sync`) never clears the TTL; the hourly reaper then deletes the live clone directory with unmerged commits and uncommitted work. The sibling `worktrees.find_expired` (`src/gobby/storage/worktrees.py:451-452`) filters `status = 'merged'`, proving the intended contract. (Synthesizer-verified the query, `record_sync`, the merge_clone caller, and the rmtree loop.)
- **Why it matters:** Unattended, unrecoverable data loss by a background loop.
- **Minimal fix:** Add `AND status = %s` (MERGED) and `AND agent_session_id IS NULL` to both branches of `clones.find_expired`; clear `cleanup_after` in `record_sync`/`claim`.
- **Confidence:** high

### [BLOCKER] Dispatch lease admits two logical holders — same-holder reacquisition of an active no-run lease with a process-constant holder string
- **Where:** `src/gobby/storage/tasks/_dispatch_mutex.py:116-121` (unexpired lease refused only when `existing_holder != holder` or attached `run_id` differs); holder is the constant `"dispatcher"` (`src/gobby/dispatch/constants.py:6`) and `RuntimeDispatchMutex.__enter__` always acquires with `run_id=None` (`src/gobby/storage/tasks/_runtime_mutex.py:53-60`); same flaw for stage mutators: `holder = by_session_id or "system"` (`src/gobby/storage/tasks/_stage_state_transitions.py:51`) with HTTP stage routes passing `by_session_id=None`
- **Failure mode:** Heartbeat H1 acquires the lease for task T and spends seconds in worktree/spawn side effects before `attach(run_id)`. Heartbeat H2 (cron tick, automation loop, and `dispatch_tick` kick bursts are independent concurrent entry points) calls `acquire_mutex` with the same constant holder and `run_id=None`; the run_id guard can't fire pre-attach, so H2's upsert (`:123-137`) overwrites H1's lease and returns True. Both spawn agents for the same stage — the documented dogfood incident (double backend-developer spawn); the #15458 fix closed only the post-attach window. Concurrent "system"-holder stage transitions are likewise never serialized against each other. (Synthesizer-verified acquire logic, constants, `run_id=None`, and the "system" fallback.)
- **Why it matters:** Two writers in one worktree, double-incremented attempt counters, duplicated lifecycle events; the dispatcher trusts this lease completely.
- **Minimal fix:** Unique per-acquisition holder token (e.g. `f"dispatcher:{uuid4()}"`) carried in `RuntimeDispatchMutex`; refuse any unexpired lease with a different token; release/refresh/attach by token.
- **Confidence:** high

### [BLOCKER] `update_task` silently swallows unknown kwargs — MCP `start_date`/`due_date` (and `verification`/`sequence_order`) updates are silent no-ops
- **Where:** `src/gobby/storage/tasks/_updates.py:269` (`**kwargs: Any` checked only for legacy/blocked names at `:272-305`, then dropped); no `start_date`/`due_date` write path anywhere in `_updates.py` despite real columns (`postgres_baseline_schema.sql:338-339`) and the `Task` model reading them (`_models.py:208-209,304-305`); live bitten callers `src/gobby/mcp_proxy/tools/tasks/_crud.py:149-156` (create flow's post-create `update_task(task.id, start_date=..., due_date=...)`) and `:495-501` (update flow forwards `verification`, `sequence_order`, `start_date`, `due_date`)
- **Failure mode:** The MCP tools advertise these parameters, accept them, report success — and nothing is persisted; the returned task even shows `start_date: None`. `verification`/`sequence_order` don't have columns at all yet are accepted and dropped the same way. Every scheduling write since these tools shipped has been lost without an error. (Synthesizer-verified the kwargs sink, both callers, the columns, and the model reads.)
- **Why it matters:** An operation that reports success while violating its contract, on the primary task-mutation API.
- **Minimal fix:** Raise `ValueError` on any unknown kwarg in `update_task_metadata`; add real write support for `start_date`/`due_date` in `_updates.update_task`.
- **Confidence:** high

### [BLOCKER] `TaskAffectedFileManager.set_files` catches IntegrityError inside an open Postgres transaction — silent total rollback reported as success
- **Where:** `src/gobby/storage/task_affected_files.py:65-86` (DELETE for same source at `:66-69`, per-file INSERT loop with `except psycopg.IntegrityError` swallow at `:83-85`); `UNIQUE(task_id, file_path)` at `postgres_baseline_schema.sql:1320-1326`
- **Failure mode:** A file already tracked under another source (reachable: `_crud.py:144-147` writes `manual`, expansion writes `expansion`) — or a duplicate within the input list — raises UniqueViolation; the except swallows it but Postgres has already aborted the transaction. If more files follow, the next INSERT raises `InFailedSqlTransaction` (not caught) and everything rolls back. If the colliding file is last, the loop completes, `return results` runs, and the COMMIT is silently a ROLLBACK — `set_files` returns a populated list while nothing was written, including the DELETE. Under ambient transaction reuse this also poisons the caller's outer transaction. `_creation.py:113` uses savepoints for exactly this pattern. (Synthesizer-verified.)
- **Why it matters:** Data loss reported as success on the file-contention path build dispatch relies on; can corrupt unrelated outer transactions.
- **Minimal fix:** `INSERT ... ON CONFLICT (task_id, file_path) DO NOTHING RETURNING ...`, or per-INSERT savepoints as in `_create_task_in_transaction`.
- **Confidence:** high

### [BLOCKER] Reparenting has no self/descendant cycle guard — a committed parent cycle crashes path materialization and hangs ancestor CTEs
- **Where:** `src/gobby/storage/tasks/_updates.py:93-95` (`parent_task_id` written with zero validation; not in the blocked-fields list at `:274-286`); `src/gobby/storage/tasks/_path_cache.py:84-108` (`update_descendant_paths` recurses parent→child with no visited set); `src/gobby/storage/tasks/_queries.py:98-108` and `_aggregates.py:61-70` (`WITH RECURSIVE ancestors ... UNION ALL`, no cycle termination); MCP exposes `parent_task_id` updates with only ref resolution (`mcp_proxy/tools/tasks/_crud.py:441,476-487`)
- **Failure mode:** `update_task(task_id, parent_task_id=<self or own descendant>)` commits the cyclic edge. `update_descendant_paths` then recurses forever (RecursionError, after the corrupt tree is durable); `compute_path_cache` bails at max_depth=100 returning None so `path_cache` silently stays stale (`_path_cache.py:35-58`). Every subsequent ready/blocked query whose blocker sits inside the cycle runs a non-terminating recursive ancestor CTE. No test covers reparent-cycle rejection. (Synthesizer-verified the unvalidated write, the unguarded recursion, the CTE, and the depth-bail.)
- **Why it matters:** One bad tool call corrupts the task tree and degrades into daemon-wide query hangs on dispatch-critical paths.
- **Minimal fix:** In `update_task_metadata`, reject `parent == task_id` and walk the new parent's ancestor chain rejecting cycles inside the same transaction; add `CYCLE`/depth guards to the recursive ancestor CTEs.
- **Confidence:** high

---

The Importants below are grouped by subsystem. They were verified by the area reviewers with file:line evidence; the synthesizer spot-checked but did not independently re-verify each.

### [IMPORTANT] `claim_task` is a lock-less check-then-act — two sessions can both "successfully" claim the same task
- **Where:** `src/gobby/storage/tasks/_transitions.py:133-154` (read `:141-142`, check `:146-147`, unconditional write `:149-153`); same pattern in `escalate_task` (`:252-258`)
- **Failure mode:** Two concurrent claims both read `claimed_by_session_id IS NULL`, both write; last writer wins, both callers receive a Task asserting ownership. No transaction or lock spans check+write.
- **Why it matters:** Task ownership is the core mutual-exclusion primitive for sessions/agents.
- **Minimal fix:** Single conditional UPDATE with rowcount check, raising on 0 rows.
- **Confidence:** high

### [IMPORTANT] Labels and commits arrays are read-modify-write without locking — concurrent writers silently lose entries (including `covers:` labels)
- **Where:** `src/gobby/storage/tasks/_lifecycle.py:104-123` (`add_label`/`remove_label`), `:153-164`/`:193-214` (`link_commit`/`unlink_commit`); `src/gobby/storage/tasks/_transitions.py:492-502` (`submit_for_review` rewrites the full labels array from a stale read)
- **Failure mode:** Each helper fetches the whole JSONB array, mutates in Python, writes it back in a fresh transaction; concurrent add_label calls (dispatcher audit marker vs agent `covers:` label) last-writer-win.
- **Why it matters:** `covers:<plan>:<section>:<item>` labels are load-bearing for the plan-coverage gate; lost commit links break close provenance. Loss is silent.
- **Minimal fix:** Atomic JSONB ops (`labels || %s::jsonb` guarded by `NOT labels @> %s::jsonb`, `labels - %s`) or `SELECT ... FOR UPDATE`.
- **Confidence:** high

### [IMPORTANT] `task_artifacts` merge-upsert clobbers concurrent field writes; `increment_expansion_attempts` loses increments
- **Where:** `src/gobby/storage/tasks/_artifacts.py:178-213` (reads current row without `FOR UPDATE`, writes every allow-listed column from the stale snapshot), `:296-300` (read via `get_artifacts` outside the write transaction)
- **Failure mode:** Writer A sets `plan_file_path` while B sets `target_branch`; the second commit reverts the first writer's field. Two concurrent increments produce +1 instead of +2, so max-attempt escalation fires late or never.
- **Why it matters:** Artifacts are the dispatcher's sparse pointers; the contract requires related fields written atomically, but cross-writer isolation is absent.
- **Minimal fix:** `SELECT ... FOR UPDATE` inside `_set_artifacts_in_transaction`; increment as `SET expansion_attempts = expansion_attempts + 1 RETURNING`.
- **Confidence:** high

### [IMPORTANT] `add_dependency` cycle check is non-transactional check-then-act — concurrent adds can commit a dependency cycle
- **Where:** `src/gobby/storage/task_dependencies.py:53-82` (cycle walk at `:61` outside the insert transaction at `:68`; `_would_create_cycle` at `:118-142` uses independent reads)
- **Failure mode:** Session 1 adds A→B while session 2 adds B→A; each walk misses the other's pending edge; both commit. A and B then block each other forever and drop out of `list_ready_tasks` with no error.
- **Why it matters:** Silent cycles stall automation subtrees indefinitely.
- **Minimal fix:** Advisory lock (like `TaskSeqAllocation`) or `transaction_immediate` with a lock around check+insert.
- **Confidence:** high

### [IMPORTANT] `priority=0` (critical) filter is silently ignored by truthiness checks
- **Where:** `src/gobby/storage/tasks/_queries.py:171-173` (`if priority:` in `list_tasks`), `:291-293` (`list_ready_tasks`); `PRIORITY_MAP["critical"] == 0` (`_models.py:21`); `_search.py:228` does it correctly (`is not None`)
- **Failure mode:** Filtering for critical tasks returns all priorities as if unfiltered — the worst failure shape for "show me only critical work".
- **Minimal fix:** `if priority is not None:` in both query builders.
- **Confidence:** high

### [IMPORTANT] LIKE patterns built from user input without `%`/`_` escaping (task queries and prefix resolution)
- **Where:** `src/gobby/storage/tasks/_queries.py:197-199` (`title LIKE`), `src/gobby/storage/tasks/_read.py:45,56` (`id LIKE prefix%`)
- **Failure mode:** `_`/`%` in the fragment act as wildcards; `find_task_by_prefix` can return a single wrong task as a "unique match", which then feeds destructive operations (close/delete by ref).
- **Minimal fix:** Escape `\`, `%`, `_` and add `ESCAPE '\'`.
- **Confidence:** high

### [IMPORTANT] `delete_task` cascade is not atomic and orphans children with stale `path_cache`
- **Where:** `src/gobby/storage/tasks/_lifecycle.py:297-350` (one transaction per node; `UPDATE tasks SET parent_task_id = NULL` at `:349` with no path recompute)
- **Failure mode:** Crash mid-cascade leaves a partially deleted subtree committed; orphaned children keep a `path_cache` containing the deleted parent's prefix, which path-based ref resolution (`_id.py:82-89`) and the cascade's own ancestor-skip check then trust.
- **Minimal fix:** Single `WITH RECURSIVE ... DELETE` transaction; recompute `path_cache` for orphans in the same transaction.
- **Confidence:** high

### [IMPORTANT] `count_ready_tasks` disagrees with `list_ready_tasks` — count ignores the parent-chain readiness requirement
- **Where:** `src/gobby/storage/tasks/_aggregates.py:166-177` (flat predicate; also counts `in_progress` while the docstring at `:150-154` says it doesn't) vs `_queries.py:252-281` (recursive CTE requiring every ancestor ready)
- **Failure mode:** Dashboards show "N ready" while the list returns fewer; the docstring lies in both directions.
- **Minimal fix:** Reuse the ready-chain CTE in the count; fix the docstring.
- **Confidence:** high

### [IMPORTANT] `list_tasks` hierarchical ordering is applied after SQL pagination — pages violate the parent-before-child contract
- **Where:** `src/gobby/storage/tasks/_queries.py:209-221` (SQL LIMIT/OFFSET first, `order_tasks_hierarchically` on the truncated page)
- **Failure mode:** With the default `sort_by="hierarchy"` and more rows than `limit`, children land on pages without their parents; `list_ready_tasks` deliberately does the opposite (fetch-then-order-then-slice), so the two surfaces paginate incompatibly.
- **Minimal fix:** Fetch-then-order-then-slice for hierarchy sort, or document per-page-only ordering.
- **Confidence:** med

### [IMPORTANT] Silent truncation at `internal_limit = 1000` in `list_ready_tasks`/`list_blocked_tasks` despite comments claiming "no SQL limit"
- **Where:** `src/gobby/storage/tasks/_queries.py:301-304`, `:352-355`
- **Failure mode:** Past 1000 ready/blocked tasks, results are truncated before hierarchical ordering and the user's offset; `offset >= 1000` silently returns `[]`. This repo itself has >15k tasks.
- **Minimal fix:** Remove the cap or surface truncation; fix the comment.
- **Confidence:** med

### [IMPORTANT] Reparent commits the parent edge and path recomputation in separate transactions — crash leaves the subtree with silently stale `path_cache`
- **Where:** `src/gobby/storage/tasks/_manager.py:414-417` (path update after `update_task_metadata` committed); `_path_cache.py:96-108` (per-row autocommit updates, N+1 recursion)
- **Failure mode:** Crash between/during leaves descendants pointing at the old ancestor chain; nothing detects or repairs; path-based resolution then misresolves.
- **Minimal fix:** Parent write + set-based recursive-CTE path recompute in one transaction.
- **Confidence:** high

### [IMPORTANT] Stage-state search swallows all exceptions and returns empty results
- **Where:** `src/gobby/storage/tasks/_search.py:206-210` (`except Exception ... return []`)
- **Failure mode:** SQL bugs, missing extension, or connection failures surface as "no matching tasks"; agents mandated to "search for an existing task before creating one" then create duplicates.
- **Minimal fix:** Catch only narrow expected errors; re-raise the rest.
- **Confidence:** med

### [IMPORTANT] `attach_run_id` updates run_id with no holder or lease-liveness guard
- **Where:** `src/gobby/storage/tasks/_dispatch_mutex.py:164-176`
- **Failure mode:** A stale dispatcher (lease expired or overwritten) clobbers the live lease's `run_id`; the live run's `refresh_mutex_for_run` (which correctly guards on holder+run_id, `:178-203`) starts failing, terminal cleanup matches the wrong lease.
- **Minimal fix:** Add holder + liveness predicates to the UPDATE; treat rowcount 0 as attach failure.
- **Confidence:** high

### [IMPORTANT] `release_mutex` deletes by non-unique holder — a stale holder's release kills a newer live lease
- **Where:** `src/gobby/storage/tasks/_dispatch_mutex.py:140-146`; exercised via `RuntimeDispatchMutex.release()` on dispatcher exception paths
- **Failure mode:** H1's lease expires mid-action; H2 legitimately takes over with the same constant holder; H1's release then deletes H2's live lease, reopening the task to a third heartbeat.
- **Minimal fix:** Same as the lease Blocker — unique per-acquisition token checked on release.
- **Confidence:** high

### [IMPORTANT] Stage transition crashes permanently once `artifact_refs` is stored as `{}` (dict passed as SQL parameter)
- **Where:** `src/gobby/storage/tasks/_stage_state_transitions.py:79-83` (`row.artifact_refs and json.dumps(...)` — `{}` short-circuits to the raw dict, bound at `:102`); `{}` reachable via MCP `artifact_updates={}` (`mcp_proxy/tools/tasks/_stage_ops.py:269-294`)
- **Failure mode:** `_coerce_artifact_refs("{}")` yields `{}`; the truthiness short-circuit passes the raw dict as the `%s` parameter; psycopg raises `cannot adapt type 'dict'` on every subsequent transition for that stage row — a stuck pipeline the dispatcher can't route out of.
- **Minimal fix:** `json.dumps(row.artifact_refs, sort_keys=True) if row.artifact_refs is not None else None`.
- **Confidence:** high

### [IMPORTANT] `_insert_future_stages` repositioning can hit the unique `(task_id, position)` index and abort `initialize_manifest`
- **Where:** `src/gobby/storage/tasks/_stage_state_manifest_ops.py:241-265` (descending-target-order UPDATE loop is only collision-free when positions move up); unique index at `postgres_baseline_schema.sql:1905-1906`
- **Failure mode:** A position-shrinking respec the validator explicitly permits (`:201-228`) raises UniqueViolation mid-loop; the transaction aborts on a shape its own validator approved, aborting build cascades mid-subtree.
- **Minimal fix:** Two-phase reposition through a disjoint temporary range (e.g. negative positions) in the same transaction.
- **Confidence:** med

### [IMPORTANT] Build cascade is non-atomic and silently partial — automation flags commit before manifests; per-task failures are swallowed or abort the loop
- **Where:** `src/gobby/storage/tasks/_build_cascade.py:51-90` (flags transaction), `:100-131` (manifest loop; `DispatchMutexUnavailableError` swallowed at `:120-127`; `if specs:` guard skips manifest creation entirely)
- **Failure mode:** (a) A busy child keeps a stale/missing manifest with automation enabled, never retried. (b) Any other exception aborts the loop leaving later siblings flagged but unshaped. (c) A task with `allow_automation=true` and zero `task_stage_states` rows is invisible to `list_automation_candidates` (INNER JOIN, `_automation.py:43-49`) — silently stuck forever.
- **Why it matters:** `gobby build` is the single automation entry point; partial cascades produce undetectable stuck subtrees.
- **Minimal fix:** Don't enable automation on a child until its manifest write succeeds; surface/retry failures in the cascade result.
- **Confidence:** high

### [IMPORTANT] `_remove_pristine_omitted_stages_for_build_cascade` mutates manifests without the dispatch mutex
- **Where:** `src/gobby/storage/tasks/_build_cascade.py:147-210` (lock-free reads at `:147-160`, deletes/renumbers at `:177-210`)
- **Failure mode:** Every other manifest mutator runs inside `RuntimeDispatchMutex` with a snapshot check; this helper races the dispatcher, which can `start_stage` or spawn onto a row being deleted out from under it.
- **Minimal fix:** Acquire the task's `RuntimeDispatchMutex` around the whole check+prune, mirroring `add_stage`/`remove_stage`.
- **Confidence:** high

### [IMPORTANT] `update_run_context` is a lock-less read-modify-write on `summary_json`
- **Where:** `src/gobby/storage/build_history.py:152-174` (read at `:159` outside the transaction, full-JSON write-back at `:163-173`)
- **Failure mode:** Concurrent context updates last-writer-win; `summary_json.coordinator_session_id` is load-bearing for `latest_coordinated_run_for_task` (`:284-321`), so losing it changes build coordination decisions.
- **Minimal fix:** SQL-side merge (`summary_json = COALESCE(summary_json,'{}')::jsonb || %s::jsonb`) or `FOR UPDATE`.
- **Confidence:** high

### [IMPORTANT] Toggling `enabled` on a bundled build profile permanently freezes it out of bundled refreshes
- **Where:** `src/gobby/storage/build_profiles.py:101-108` (drift gate), `:593-605`/`:631-643` (`enabled` in both hash payloads), `:443-460` (`upsert_installed` overwrites `enabled` on refresh)
- **Failure mode:** Disabling a bundled profile makes its hash differ from the template's, so sync classifies it user-edited and skips it forever; if a refresh does run, the user's toggle is overwritten. Both directions violate the CLAUDE.md templates contract (refresh on drift, preserve enabled).
- **Minimal fix:** Exclude `enabled` from drift hashes; preserve the existing `enabled` in `upsert_installed`.
- **Confidence:** med

### [IMPORTANT] `_cascade_close_descendants` overwrites closure metadata of already-closed descendants
- **Where:** `src/gobby/storage/tasks/_stage_utils.py:182-206` (recursive UPDATE has no `closed_at IS NULL` guard); triggered on every merge-stage completion (`_stage_state_transitions.py:148-159`)
- **Failure mode:** Descendants closed days earlier get `closed_reason='merged'`, a new `closed_at`, and the merge SHA — original closure provenance destroyed on a mainline path (epic merge).
- **Minimal fix:** `AND closed_at IS NULL` in the WHERE clause.
- **Confidence:** high

### [IMPORTANT] The load-bearing stage-state concurrency "test" is a symbol-existence stub
- **Where:** `tests/storage/tasks/test_stage_states_concurrency.py:14-20` via `tests/phase2_stage_contract_helpers.py:30-71` (asserts paths/symbols/text exist, nothing behavioral)
- **Failure mode:** Plan acceptance names `test_mutex_serializes_writes` as proof concurrent `start_stage` calls serialize; no test anywhere exercises two concurrent same-task mutators — exactly the contract the lease Blocker breaks.
- **Minimal fix:** Real two-writer test (distinct holders and the same-holder "system" case) asserting exactly one transition wins.
- **Confidence:** high

### [IMPORTANT] Cross-project session recovery move can violate `idx_sessions_seq_num`, permanently breaking registration for that session
- **Where:** `src/gobby/storage/sessions/_crud.py:146-149` (move keeps the old project's `seq_num`), `:245-250` (conflict handler wraps only the INSERT; `is_session_unique_conflict` deliberately rejects `idx_sessions_seq_num`); unique index at `postgres_baseline_schema.sql:262`
- **Failure mode:** Moving a session into a project that already has that seq raises a unique violation; registration aborts, `register_session` recovery fails, returns `""` — and every subsequent registration of that external session retries the same failing move. Transcripts, usage, lineage, and handoffs for that CLI session are silently lost. No behavioral test covers the move.
- **Minimal fix:** Re-mint `seq_num` in the destination project under `SessionSeqMutation(destination)` in the same UPDATE.
- **Confidence:** high

### [IMPORTANT] `update(project_id=...)` single-session move has the same seq_num-collision hazard
- **Where:** `src/gobby/storage/sessions/_bulk_update.py:109-123`
- **Failure mode:** Project move without seq remap or `SessionSeqMutation`; collision raises; even success carries a foreign seq into the destination's numbering.
- **Minimal fix:** Re-mint `seq_num` under the destination lock in the same transaction.
- **Confidence:** high

### [IMPORTANT] Lineage-cycle guard is check-then-act — concurrent parent updates can commit a session lineage cycle
- **Where:** `src/gobby/storage/sessions/_field_update.py:335-357` (`update_parent_session_id`, plain transaction); walk at `sessions/_lineage_guard.py:67-91`; DB CHECK blocks only self-parent
- **Failure mode:** T1 sets A.parent=B while T2 sets B.parent=A; both chain-walks see no cycle; both commit → A↔B. Package-internal readers carry `seen` guards but lineage semantics (walk to root) break for ancestry/handoff and external consumers.
- **Minimal fix:** Advisory lock serializing parent mutations, or `FOR UPDATE` on chain rows during the walk.
- **Confidence:** med

### [IMPORTANT] Concurrent registration with unknown vs known project_id creates duplicate sessions for one external session
- **Where:** `src/gobby/storage/sessions/_crud.py:120-126` (registration advisory lock key includes `project_id`), `:129-156` (recovery scan), `:190-244` (insert)
- **Failure mode:** register(external, `_personal`) and register(same external, project X) take different locks and run concurrently; B can't see A's uncommitted row; two Gobby sessions for one CLI session; `resolve_session_reference` then raises "Ambiguous session reference" and hook events split across the rows permanently.
- **Minimal fix:** Derive the lock from project-independent identity (external_id|machine_id|source|session_type).
- **Confidence:** med

### [IMPORTANT] `recover_session` ambiguity guard is dead code — ties are silently resolved instead of refused
- **Where:** `src/gobby/storage/sessions/_registration_cache.py:37-39` (`_recovery_rank` ends with unique `session.id`), `:241-252` (equality check on the full rank can never be true)
- **Failure mode:** Two equally-complete cross-source candidates always silently resolve to the lexicographically-first id — the silent nondeterminism the guard was written to prevent; transcript/usage data then lands on the wrong session.
- **Minimal fix:** Compare score-without-id for ambiguity; keep the full rank only as the sort key.
- **Confidence:** high

### [IMPORTANT] Session prefix resolution passes raw user input into LIKE without escaping
- **Where:** `src/gobby/storage/session_resolution.py:126,134-141`
- **Failure mode:** `_` (common in external IDs) matches any character; `ref="%"` matches everything; silent wrong-session resolution redirects updates/summaries/deletes.
- **Minimal fix:** Escape `\`, `%`, `_` with `ESCAPE '\'` (shared helper — same fix as the tasks/prompts/model_costs instances).
- **Confidence:** high

### [IMPORTANT] Token events with NULL message_id have no idempotency — transcript replay double-counts usage
- **Where:** `src/gobby/storage/token_events.py:153` (`ON CONFLICT ... WHERE message_id IS NOT NULL DO NOTHING`); NULL producers `sessions/transcripts/droid.py:150-151` (and `claude.py:709` fallback); replay triggers `sessions/_transcript.py:64-79`, `_field_update.py:51-81`
- **Failure mode:** Reprocessing a transcript from offset 0 re-inserts every NULL-message_id event, inflating `get_session_totals` and all downstream usage/cost accounting, silently and cumulatively.
- **Minimal fix:** Synthesize a deterministic message_id (transcript path + line index hash) at parse time.
- **Confidence:** med

### [IMPORTANT] Agent run `start()` has no status guard — resurrects cancelled/terminal runs
- **Where:** `src/gobby/storage/agents/_lifecycle.py:116-127` (unconditional UPDATE; terminal transitions at `:188-321` all guard `status IN ('pending','running')`)
- **Failure mode:** Cancel lands between spawn's `pending` insert and `start()`; `start()` flips it back to `running`: sessions already expired, daemon-stop resume no longer matches, `has_active_run_for_task` blocks new spawns, and the zombie occupies a `count_active_agents` cap slot until the 30-minute stale sweep.
- **Minimal fix:** `AND status = 'pending'` with rowcount check, like the other transitions.
- **Confidence:** high

### [IMPORTANT] Expansion-run state machine has no transition guards — late writers resurrect cancelled/failed runs
- **Where:** `src/gobby/storage/expansion_runs.py:234-403` (`start`, `save_compiled_spec`, `mark_applying`, `save_apply_result`, `fail`, `cancel` — all `WHERE id = %s` only)
- **Failure mode:** User cancel races the background worker; the worker's later writes overwrite `cancelled` back to `compiled`/`completed`; conversely `cancel` clobbers a just-completed run. Dispatch hooks then fire on the wrong terminal state.
- **Minimal fix:** Status predicates per transition; return None on zero rowcount.
- **Confidence:** high

### [IMPORTANT] Orphaned expansion runs after a hard daemon crash are never reaped — permanently block re-expansion
- **Where:** `src/gobby/storage/expansion_runs.py:201-214` (`get_active_for_task`); gate at `mcp_proxy/tools/tasks/_expansion.py:349-358`; no startup sweep exists (contrast `storage/agents/_cleanup.py`)
- **Failure mode:** SIGKILL mid-expansion leaves rows `running`/`applying` forever; `start_expansion_run` without `force_new` returns the dead run forever.
- **Minimal fix:** `cleanup_stale_runs`-style sweeper invoked from startup/maintenance, mirroring agent runs.
- **Confidence:** high

### [IMPORTANT] Deterministic merge-resolution ID omits `target_branch` — second-target merges hit an unrecoverable IntegrityError dead-end
- **Where:** `src/gobby/storage/merge_resolutions.py:177` (`generate_prefixed_id("mr", worktree_id + source_branch)`), `:225-266` (`get_or_create_resolution` re-fetch keyed on worktree+source+target)
- **Failure mode:** Merging the same branch to a second target: INSERT collides on the PK with the target-A row, the target-B re-fetch returns None, re-raise — permanent dead-end until the target-A row is manually deleted. Same-target re-merge returns a stale `resolved` row instead of a fresh pending one.
- **Minimal fix:** Include `target_branch` in the hash, or random IDs + a real UNIQUE on (worktree, source, target).
- **Confidence:** high

### [IMPORTANT] `update_resolution`/`update_conflict` are lock-less read-modify-write — concurrent resolvers revert terminal states
- **Where:** `src/gobby/storage/merge_resolutions.py:282-317`, `:452-497` (get → Python merge → unconditional full-column UPDATE)
- **Failure mode:** AI tier and human CLI resolving concurrently: the slower writer reverts `resolved` to `pending` with stale content — on the table that exists to coordinate exactly this.
- **Minimal fix:** SQL-side `COALESCE` merge in one statement and/or expected-status predicates.
- **Confidence:** high

### [IMPORTANT] `claim()` is last-writer-wins for both worktrees and clones
- **Where:** `src/gobby/storage/worktrees.py:329-340`; `src/gobby/storage/clones.py:394-405`; racing check-then-act caller `mcp_proxy/tools/clones.py:622-636`
- **Failure mode:** Two sessions both pass the ownership check and both get success; the second silently steals the directory; two agents mutate the same working tree.
- **Minimal fix:** Conditional UPDATE (`WHERE agent_session_id IS NULL OR agent_session_id = %s`) with rowcount check.
- **Confidence:** high

### [IMPORTANT] `worktrees.find_stale` uses `updated_at` as an activity proxy and ignores ownership — cleanup can abandon/force-delete a worktree in active use
- **Where:** `src/gobby/storage/worktrees.py:399-430` (no `agent_session_id` guard; contrast `clones.py:468-469`); nothing bumps the row on reuse (`agents/isolation.py:220-244`); blast radius `mcp_proxy/tools/worktrees/_cleanup.py:127-158` (force path deletes directory and branch)
- **Failure mode:** A worktree re-entered by an agent today but created days ago is marked abandoned; the force-delete path destroys uncommitted work and the branch. Non-default flags required, hence Important not Blocker.
- **Minimal fix:** Exclude claimed worktrees; bump `updated_at` (or add `last_activity_at`) on reuse.
- **Confidence:** high

### [IMPORTANT] Clone stuck in `syncing` after a crash is invisible to every cleanup path
- **Where:** `src/gobby/storage/clones.py:333-343` (`mark_syncing`), `:464-475` (`find_stale` matches only `active`); crash window `mcp_proxy/tools/clones.py:484-500`
- **Failure mode:** Crash between `mark_syncing` and the reset leaves the row `syncing` forever; no reaper or startup reconciliation touches it; `get_by_branch` keeps returning the wedged clone.
- **Minimal fix:** Include time-thresholded `syncing` in `find_stale` or reset on startup.
- **Confidence:** high

### [IMPORTANT] `clones.get_by_task` returns an arbitrary row when a task has multiple clones
- **Where:** `src/gobby/storage/clones.py:202-205` (no ORDER BY, `fetchone`); the worktree twin was deliberately fixed with status-priority + recency ordering (`worktrees.py:187-213`)
- **Failure mode:** A task with a dead clone plus a fresh active one nondeterministically resolves to the dead one's path.
- **Minimal fix:** Copy the worktrees ORDER BY.
- **Confidence:** high

### [IMPORTANT] `worktrees.sync_with_merge_resolution`/`sync` report success without performing any sync
- **Where:** `src/gobby/storage/worktrees.py:570-614` (hard-coded `{"success": True, ... "Sync completed without conflicts"}`; self-described placeholder)
- **Failure mode:** No production caller yet, but the first consumer inherits a false-success no-op merge sync on a data-bearing operation.
- **Minimal fix:** Delete or raise `NotImplementedError`.
- **Confidence:** high

### [IMPORTANT] Stored agent PID used for liveness and SIGTERM with no reuse protection; PID never cleared on terminal transitions
- **Where:** persisted `src/gobby/storage/agents/_runtime.py:100-140` (never nulled by `_lifecycle.py` terminal transitions); consumed `src/gobby/agents/agent_health.py:158-170` (`os.kill(pid, 0)`), `:214-220`, `:283-287` (SIGTERM)
- **Failure mode:** After PID reuse, the liveness probe reports a dead agent alive (cleanup never fires) and the timeout path SIGTERMs an unrelated process.
- **Minimal fix:** Null `pid` in terminal UPDATEs; record process start time and validate identity before signaling.
- **Confidence:** med

### [IMPORTANT] Run-replay "pagination" loops re-fetch the same first page — silent truncation beyond 500 rows
- **Where:** `src/gobby/runner_lifecycle_agents.py:64-86`, `:191-204`, `:245-301` (`while True` with no offset; dedupe set terminates the loop after one page); `list_by_status` lacks an offset parameter (`storage/agents/_queries.py`)
- **Failure mode:** Startup reconciliation/notification silently skips everything past the first 500 rows; cancelled rows accumulate unboundedly so older daemon-restart cancellations drop out of replay.
- **Minimal fix:** Add offset to `list_by_status` or keyset-paginate.
- **Confidence:** high

### [IMPORTANT] Expired-worktree reaper runs `git worktree remove`/`git branch -D`/`git worktree prune` in the daemon's CWD, not the worktree's parent repo
- **Where:** `src/gobby/runner_maintenance.py:576-592` with `_run_git_command` at `:738-743` (`subprocess.run` with no `cwd`)
- **Failure mode:** Best case the commands fail silently (branch leak, worktree metadata never pruned); worst case the daemon's CWD is another git repo and `git branch -D <name>` deletes a same-named branch in the wrong repository.
- **Minimal fix:** Resolve the repo path from the worktree row's project and pass `cwd`/`git -C`; treat failures as errors.
- **Confidence:** med

### [IMPORTANT] No tests cover the destructive sweep criteria (`find_expired`/`find_stale`) for worktrees or clones
- **Where:** `tests/storage/test_worktrees.py`, `tests/storage/test_clones.py` (zero references; the clones tests are mock-based and assert SQL was called, not row selection)
- **Failure mode:** The exact queries deciding what gets rmtree'd are untested — the clone-reaper Blocker would have been caught by one real-DB test mirroring the worktrees contract.
- **Minimal fix:** Real-DB tests asserting both sweeps exclude active/claimed rows.
- **Confidence:** high

### [IMPORTANT] Global content-keyed memory dedup silently suppresses per-project memories
- **Where:** `src/gobby/storage/memories.py:153-160` (uuid5 ID from content only; early-return of the existing row), `:261-306` (global `content_exists`/`get_memory_by_content`)
- **Failure mode:** Project A stored content X; `create_memory(X, project_id=B)` returns A's memory and never creates B's row; B's recall filters (`project_id = B OR NULL`) can never see it — write reports success, fact permanently unreachable in B. Borderline Blocker; graded Important because intent comments suggest deliberate global dedup.
- **Minimal fix:** Include `project_id` in the uuid5 seed and dedup checks, or clone into the requesting scope.
- **Confidence:** high

### [IMPORTANT] `create_memory` check-then-act race crashes with raw UniqueViolation
- **Where:** `src/gobby/storage/memories.py:159-160` (existence check in its own transaction) vs `:180-201` (INSERT later)
- **Failure mode:** Concurrent same-content creates both pass the check; the loser gets a raw UniqueViolation instead of the promised dedup-return.
- **Minimal fix:** `INSERT ... ON CONFLICT (id) DO NOTHING` then fetch, one transaction.
- **Confidence:** high

### [IMPORTANT] `update_memory` breaks the content-addressed ID invariant — later creates return wrong content or duplicate rows
- **Where:** `src/gobby/storage/memories.py:308-339` (content update keeps the old uuid5 id)
- **Failure mode:** After editing content, `create_memory(OLD)` returns a Memory whose content is NEW (nothing stored); `create_memory(NEW)` inserts a duplicate row. Dedup silently degrades after any content edit.
- **Minimal fix:** Delete+recreate on content change, or dedup on a stored normalized-content hash column.
- **Confidence:** high

### [IMPORTANT] Content updates never reset `graph_processed` — knowledge graph keeps stale extraction
- **Where:** `src/gobby/storage/memories.py:308-339`; manager confirms no compensation (`memory/manager.py:624-642` re-embeds but never re-enqueues for graph)
- **Failure mode:** Vector store refreshes, FalkorDB keeps entities/relations from the old content indefinitely; graph-boosted recall returns relations no stored memory contains.
- **Minimal fix:** `graph_processed = FALSE` in the same UPDATE whenever content changes.
- **Confidence:** high

### [IMPORTANT] Tag-filtered memory list/search silently drop matches and corrupt pagination
- **Where:** `src/gobby/storage/memories.py:455-470`, `:522-535` (SQL `LIMIT limit*3 OFFSET offset`, then Python tag filter, then `[:limit]`)
- **Failure mode:** Real matches beyond the 3× window are omitted; OFFSET applies pre-filter so pages skip/duplicate rows.
- **Minimal fix:** Push tag predicates into SQL (`tags @> %s::jsonb` via the existing `json_array_contains_condition` helper).
- **Confidence:** high

### [IMPORTANT] Deterministic name-derived IDs make rename-then-recreate crash with raw PK violations (skills and prompts)
- **Where:** `src/gobby/storage/skills/_metadata.py:127` + `:338-339` (rename keeps the old `skl-` hash id); `src/gobby/storage/prompts.py:286` + `:449-450` (same for `pmt-`)
- **Failure mode:** Rename "foo"→"bar"; later `create("foo")` recomputes the identical id → raw UniqueViolation (not the documented ValueError); the name is permanently poisoned. Concurrent duplicate creates hit the same path.
- **Minimal fix:** Random IDs (generator supports `content=None`) or re-derive on rename; translate UniqueViolation to ValueError.
- **Confidence:** high

### [IMPORTANT] projects: UNIQUE(name) violations escape as raw driver errors on three real paths
- **Where:** `src/gobby/storage/projects.py:169-179` (`get_or_create` check-then-act), `:117-167` (`get_by_name` excludes soft-deleted; `create` has no conflict handling), `:181-219` (`ensure_exists` covers only `ON CONFLICT (id)`); `name TEXT NOT NULL UNIQUE` at schema:8
- **Failure mode:** (1) Soft-deleted "foo" permanently squats the name — re-init crashes. (2) Concurrent `get_or_create` crashes. (3) `ensure_exists`'s own documented cross-machine case (same name, different id) crashes during sync.
- **Minimal fix:** Partial unique index `WHERE deleted_at IS NULL` (or revive on reuse); UniqueViolation handling in `create`/`ensure_exists`.
- **Confidence:** high

### [IMPORTANT] workflow_definitions has no name uniqueness — duplicate definitions, nondeterministic get_by_name, double rule evaluation
- **Where:** `src/gobby/storage/workflow_definitions.py:126-175` (`create`, no existence check), `:379-415` (`import_from_yaml` always creates), `:187-206` (`get_by_name` fetchone, no ORDER BY); schema:1188-1213 has no UNIQUE on (name, project_id)
- **Failure mode:** Importing the same YAML twice → two rows; `list_rules_by_event` returns both → the rule engine evaluates the rule twice; toggles hit a random row while the twin keeps firing. Violates "the DB is the source of truth for installed definitions."
- **Minimal fix:** Partial unique index on (name, project_id) for live rows; upsert or friendly error in create/import.
- **Confidence:** high

### [IMPORTANT] Template drift detection (`has_template_update`) is permanently true — hash compares different JSON serializations
- **Where:** `src/gobby/storage/workflow_definitions.py:23-29` (`compute_definition_hash` hashes the raw string); template side serializes spaced (`workflows/template_hashes.py:86,105,134`) vs JSONB read-back re-serialized compact (`hub/postgres.py:374-381`); compared at `template_hashes.py:217`
- **Failure mode:** SHA-256 of spaced vs compact JSON differs for every non-empty definition, so the drift badge is on for every bundled rule/pipeline/variable forever — the operator's drift signal is no signal. (Actual sync refresh is safe: `sync_rules.py:365` compares semantically.) Confirms the low-confidence `_normalize_value` hazard noted in storage-core.md with a concrete broken consumer.
- **Minimal fix:** Canonicalize before hashing (`json.dumps(json.loads(s), sort_keys=True, separators=(",", ":"))`); test that `has_drift` is False after a Postgres round-trip.
- **Confidence:** high

### [IMPORTANT] prompts duplicate-create error translation is dead SQLite code on Postgres
- **Where:** `src/gobby/storage/prompts.py:311-321` (`if "UNIQUE constraint" in str(e)` — psycopg says "unique constraint", lowercase)
- **Failure mode:** The documented `ValueError("Prompt ... already exists")` can never be raised; callers catching ValueError get unhandled driver exceptions. No test covers the duplicate path.
- **Minimal fix:** Catch `psycopg.errors.UniqueViolation`; add the missing test.
- **Confidence:** high

### [IMPORTANT] workflow_audit_log grows unbounded — retention method exists but is never called
- **Where:** `src/gobby/storage/workflow_audit.py:359-380` (`cleanup_old_entries`; only caller is a test); writers on every workflow event (`workflows/engine/enforcement.py:92,110`); audit rows also block empty-session pruning (`session_lifecycle.py:18-25`)
- **Failure mode:** Unbounded growth on a hot write path, plus a cascading retention block: sessions referenced by audit rows are retained forever.
- **Minimal fix:** Wire `cleanup_old_entries` into the daily maintenance loop with a configurable window.
- **Confidence:** high

### [IMPORTANT] Audit `log()` swallows every exception — enforcement audit trail silently incomplete
- **Where:** `src/gobby/storage/workflow_audit.py:105-111` (`except Exception ... return None`; all `log_*` helpers inherit)
- **Failure mode:** FK violations (unregistered session) and transient DB errors silently drop block/allow decisions from the trail readers trust as complete.
- **Minimal fix:** Narrow the except to the expected FK race; let unexpected errors propagate or count drops.
- **Confidence:** high

### [IMPORTANT] `archive_plan` can silently overwrite a previously archived plan file
- **Where:** `src/gobby/storage/plans.py:222-227` (`shutil.move` with no destination-existence check)
- **Failure mode:** Archive → `create_plan` ON CONFLICT re-registers the same plan_id/path → second archive replaces the first archived document. Data loss of the historical record under `.gobby/plans/completed/`.
- **Minimal fix:** Uniquify (timestamp/hash suffix) or refuse when the destination exists.
- **Confidence:** med

### [IMPORTANT] `create_plan` ON CONFLICT silently resurrects archived plans and rewrites `root_task_ref`
- **Where:** `src/gobby/storage/plans.py:84-105` (`DO UPDATE ... state='active', root_task_ref=excluded..., archived_at=NULL`)
- **Failure mode:** A duplicate plan_id (possibly a caller mistake) flips an archived registry row active and repoints it at a different task tree with no guard or audit; coverage tooling keyed on the old root now evaluates the wrong tree.
- **Minimal fix:** Raise (or require explicit `reactivate=True`) when the conflicting row is archived or has a different root_task_ref.
- **Confidence:** med

### [IMPORTANT] Hard project delete raises FK violation instead of returning False
- **Where:** `src/gobby/storage/projects.py:268-276`; sessions/tasks/plans/memories FKs have no ON DELETE action (schema:157,294,421,675) while ~20 other tables CASCADE
- **Failure mode:** `delete()` on any non-empty project raises uncaught ForeignKeyViolation instead of the documented bool. Latent (callers use soft_delete) but the contract is unsatisfiable.
- **Minimal fix:** Pre-check/translate, or align the four FKs with the cascade policy.
- **Confidence:** high

### [IMPORTANT] `search_executions` with `search_outputs=True` crashes on Postgres (`jsonb LIKE` has no operator)
- **Where:** `src/gobby/storage/pipelines.py:424-426`, `:481-483` (`se.output_json LIKE %s` — JSONB column per schema:1125); reachable via HTTP (`servers/routes/pipelines.py:190-231`) and MCP (`_pipeline_query.py:147-189`)
- **Failure mode:** Verified live against the project's Postgres 18.4: `UndefinedFunction: operator does not exist: jsonb ~~ unknown` — a 500 on a user-togglable search flag. Tests only exercise the default False.
- **Minimal fix:** `se.output_json::text LIKE %s` in both queries; add a Postgres-backed test with the flag on.
- **Confidence:** high

### [IMPORTANT] `CronJobStorage.update_job` never recomputes `next_run_at` — re-enabling via update always raises; schedule edits fire once on the old schedule
- **Where:** `src/gobby/storage/cron.py:342-373` (validates stale `next_run_at`, never recomputes); no caller passes `next_run_at` (`cli/cron.py:249-277`, `routes/cron.py:137-157`, `mcp_proxy/tools/cron.py:224`)
- **Failure mode:** (1) Disabled jobs have `next_run_at=NULL`; `--enabled` via CLI/HTTP → `ValueError` → traceback/500; there is no caller-facing way to re-enable via update. (2) Editing `cron_expr`/`interval_seconds` leaves the old `next_run_at`, so the job fires once more at the old time (shrinking a 6h interval to 60s still waits up to 6h). Contrast `toggle_job` (`:558-566`), which recomputes.
- **Minimal fix:** Recompute via `compute_next_run(replace(job, **fields))` whenever schedule-shaping fields or `enabled` change.
- **Confidence:** high

### [IMPORTANT] `create_job` reports success for an invalid cron expression — enabled job that never fires
- **Where:** `src/gobby/storage/cron.py:88-97` (`compute_next_run` swallows croniter errors → None, no logging in the cron branch), `:177-216` (insert proceeds with `next_run_at=NULL`); no caller validates
- **Failure mode:** `gobby cron add --schedule "not-a-cron"` → row inserted `enabled=TRUE, next_run_at=NULL` → `get_due_jobs` (`:589`) never selects it. User sees "created"; zero diagnostics. Inconsistent with `update_job`, which refuses exactly this state.
- **Minimal fix:** Raise on None next-run for enabled cron jobs at create; log the croniter error.
- **Confidence:** high

### [IMPORTANT] Inter-session message delivery has no atomic claim — three independent consumers race and duplicate delivery
- **Where:** `src/gobby/storage/inter_session_messages.py:288-303` (`get_undelivered_messages`) + `:364-386` (`mark_delivered` — unconditional, no `delivered_at IS NULL` guard); consumers in `hooks/event_enrichment.py:181-192`, `mcp_proxy/tools/agent_messaging.py:262-266`, `servers/websocket/chat/_pending_messages.py:40-49`
- **Failure mode:** Hook enrichment and websocket/MCP delivery each SELECT the same undelivered rows, each inject the message, each stamp it. The recipient sees the same inter-agent message or completion wake twice — duplicate wakes can re-trigger agent behavior. Structural: the consumers live in three subsystems.
- **Minimal fix:** Atomic claim API (`UPDATE ... SET delivered_at = %s WHERE to_session = %s AND delivered_at IS NULL RETURNING *`); migrate all three consumers.
- **Confidence:** high

### [IMPORTANT] `chat_messages.save_message` allocates seq via MAX+1 RMW with no unique constraint — duplicate seq drops messages from incremental fetch
- **Where:** `src/gobby/storage/chat_messages.py:31-58` (SELECT MAX then INSERT, plain READ COMMITTED); only a non-unique index exists (schema:1634-1646)
- **Failure mode:** Concurrent saves for one conversation insert the same seq; `get_messages(after_seq=N)` then permanently skips the duplicate for any client past N — silent message loss in the layer that exists to prevent it.
- **Minimal fix:** `UNIQUE(conversation_id, seq)` + retry, or atomic `INSERT ... SELECT COALESCE(MAX(seq),0)+1` under a per-conversation advisory lock.
- **Confidence:** high

### [IMPORTANT] Comms channel `webhook_secret` is stored plaintext while every sibling secret goes through the encrypted SecretStore
- **Where:** `src/gobby/storage/communications.py:44-57`, `:85-103` (verbatim persist); manager explicitly exempts it (`communications/manager.py:574-577`) and consumes it raw (`:524-535`); contrast `github_triage` which stores `webhook_secret_ref` (schema:1572)
- **Failure mode:** Any DB dump/backup or table read yields the HMAC key needed to forge inbound Slack/Telegram webhooks — the only authentication on those endpoints.
- **Minimal fix:** Store via SecretStore and persist a `$secret:` ref, resolving at verification time.
- **Confidence:** high

### [IMPORTANT] Comms retention deletes message rows (cascading attachment rows) but never unlinks attachment files — disk leak
- **Where:** `src/gobby/storage/communications.py:312-317` (`delete_messages_before` — bare DELETE returning a count); driven daily by `runner_maintenance.py:366-390`; `comms_attachments.local_path` is the only pointer to the files (schema:1584-1597, ON DELETE CASCADE)
- **Failure mode:** Each retention pass destroys the only record of downloaded attachment files; the files persist on disk forever, unfindable. Contrast `chat_attachments.delete_stale_unbound_attachments`, which returns records for unlinking.
- **Minimal fix:** RETURNING the affected `local_path`s and unlink, mirroring the chat-attachment path.
- **Confidence:** high

### [IMPORTANT] No lease/claim on due cron jobs — two scheduler instances sharing the hub DB double-fire every job
- **Where:** `src/gobby/storage/cron.py:583-603` (`get_due_jobs` plain SELECT), `:699-711` (`has_running_run` check-then-act — and new runs insert as `pending`, so the guard misses them anyway), `:607-643` (`create_run` unconditional); scheduler advances `next_run_at` only after creating the run (`scheduler/scheduler.py:121-183`)
- **Failure mode:** Two daemons on one Postgres hub (remote DSN is supported) both fetch the same due rows and both dispatch — including the dispatcher heartbeat. Even one process has a pending-status hole if ticks overlap.
- **Minimal fix:** CAS claim (`UPDATE cron_jobs SET next_run_at = <next> WHERE id = %s AND next_run_at = <claimed>`, rowcount-checked) before creating the run; insert runs as `running`.
- **Confidence:** med

### [IMPORTANT] `refresh_tools_incremental` lowercases tool names but compares against original-case names — mixed-case tools never cached
- **Where:** `src/gobby/storage/mcp.py:704-727` (lowercased membership tests) vs `mcp_proxy/schema_hash.py:238-248` (original-case `new`/`changed` sets)
- **Failure mode:** `searchDocs` is classified "unchanged", never inserted, re-classified "new" forever — silent cache corruption. Mitigating: no production caller today (the registry-refresh route reimplements the logic consistently); only lowercase-fixture tests exercise it.
- **Minimal fix:** Normalize case on one side, or delete the method if the route's implementation is the keeper.
- **Confidence:** high

### [NIT] `TaskLifecycleEventManager.ensure_table` is SQLite DDL that can never run on the Postgres hub
- **Where:** `src/gobby/storage/tasks/_lifecycle_events.py:47-68`
- **Note:** `AUTOINCREMENT` is invalid PostgreSQL; no production caller. Delete the method.

### [NIT] `validate_category` is exported but never called — category unvalidated at the storage boundary
- **Where:** `src/gobby/storage/tasks/_models.py:72-84`; `_creation.py:151` and `_updates.py:102-104` insert raw
- **Note:** Non-MCP callers can persist arbitrary categories that `_stage_reviewer_selector.py:160` later silently treats differently. Wire in a raising validator.

### [NIT] `reject_review` round-heading replacement matches by prefix — Round 1 re-runs also rewrite Rounds 10-19
- **Where:** `src/gobby/storage/tasks/_transitions.py:593-600`
- **Note:** `^## Adversary Findings — Round 1` (no boundary) matches Round 10+; sub replaces every match. Anchor with an end boundary; stop at any `^## ` heading.

### [NIT] `update_task_with_result` is dead code with a destructive default
- **Where:** `src/gobby/storage/tasks/_decomposition.py:78-93`
- **Note:** Calling without `description` clears the task description (None forwarded, not UNSET). Only test callers exist. Delete or default to UNSET.

### [NIT] `remove_dependency` ignores `dep_type` — deletes blocks/related/discovered-from edges for the pair at once
- **Where:** `src/gobby/storage/task_dependencies.py:84-92`; schema allows multiple rows per pair (UNIQUE includes dep_type)
- **Note:** Also `add_dependency` doesn't catch UniqueViolation (raw IntegrityError poisons ambient transactions). Add a dep_type filter and idempotent duplicate handling.

### [NIT] `search_tasks` does per-hit `get_task` (3 queries each) under a "Batch-fetch" comment; `order_tasks_hierarchically` cycle fallback is O(n²)
- **Where:** `src/gobby/storage/tasks/_manager.py:945-953`; `_ordering.py:97`
- **Note:** Batch with `WHERE id IN (...)`; use an id-set for the remainder check.

### [NIT] `cascade_build_state_to_subtree` accepts and silently discards `skip_stages`
- **Where:** `src/gobby/storage/tasks/_build_cascade.py:27,43` (`_ = tuple(skip_stages)`)
- **Note:** Misleading API on the single build entry path. Drop or honor it.

### [NIT] Dead migration branch in `ensure_phase2_columns`
- **Where:** `src/gobby/storage/tasks/_stage_state_schema.py:18-20` vs `:46-51`
- **Note:** The early return makes the later backfill unreachable. Delete the dead branch.

### [NIT] `rebuild_stage_states_table` snapshots rows outside its rebuild transaction
- **Where:** `src/gobby/storage/tasks/_stage_state_schema.py:55-56`
- **Note:** Writes between the read and the DROP are lost. Legacy-migration path only. Move the SELECT inside the transaction.

### [NIT] `RuntimeDispatchMutex.release()` deletes a run-attached lease; safety depends on callers avoiding `with`
- **Where:** `src/gobby/storage/tasks/_runtime_mutex.py:73-74` (`__exit__` releases unconditionally), `:101-107` (`release()` ignores `_run_id`)
- **Note:** After `attach()`, the lease should outlive the context; any exception path calling `release()` post-attach reopens the task to dispatch while the agent lives. Make release a no-op (or guarded) when `_run_id` is set.

### [NIT] `transition(artifact_updates=...)` replaces the whole `artifact_refs` JSON despite the "updates" name
- **Where:** `src/gobby/storage/tasks/_stage_state_transitions.py:79-83,102`
- **Note:** A one-key update wipes previously stored refs (e.g. `workspace_merge.py:518`). Merge before dumping or rename the parameter.

### [NIT] `delete_stage` blocker check and soft delete are not in one transaction
- **Where:** `src/gobby/storage/tasks/_stage_registry.py:241-260`
- **Note:** A manifest referencing the stage can appear between check and soft delete; `registry_entry()` then raises for that task. Run both in one transaction with a re-check.

### [NIT] `_json_obj` decodes `summary_json` without error handling
- **Where:** `src/gobby/storage/build_history.py:436-444`
- **Note:** One corrupt row breaks `list_runs`/`get_run` for the whole project. Mirror `_json_list`'s guarded decode.

### [NIT] `StageStatesManager.__init__` performs schema introspection/DDL on every construction
- **Where:** `src/gobby/storage/tasks/_stage_states.py:61`; ad-hoc construction on hot paths (`_build_cascade.py:100`)
- **Note:** Run the ensure once at startup, not per instance.

### [NIT] Re-registration unconditionally resurrects soft-deleted sessions
- **Where:** `src/gobby/storage/sessions/_upsert.py:62` (`status = 'active'`); lookup has no status filter (`_discovery.py:101-114`)
- **Note:** A late hook event after a user-visible delete un-hides the session. Preserve `deleted` on re-registration.

### [NIT] Registration caches are never invalidated and grow unboundedly
- **Where:** `src/gobby/storage/sessions/_manager.py:93-127`; `delete()`/prune purge nothing
- **Note:** Stale `(external_id, source)` entries cause silent no-op writes in session_end hooks; slow daemon memory growth. Purge on delete/prune.

### [NIT] Dead 30-second blocking poller in the storage layer
- **Where:** `src/gobby/storage/sessions/_registration_cache.py:86-150` (`find_parent_session`, `time.sleep(1)` × 30)
- **Note:** No production caller; would block the loop if adopted. Delete it.

### [NIT] `update_parent_session_id` skips the change notification fired by every sibling mutator
- **Where:** `src/gobby/storage/sessions/_field_update.py:335-357`
- **Note:** Lineage changes never broadcast `session_updated`. Add `_notify_session_change` after commit.

### [NIT] `TaskCompactor.find_candidates` filters on `updated_at` not closed duration; `compact_task` is unguarded
- **Where:** `src/gobby/storage/compaction.py:15-52`
- **Note:** Docstring/filter mismatch; direct `compact_task` can overwrite an open task's description. Add `closed_at IS NOT NULL AND compacted_at IS NULL` + rowcount check.

### [NIT] `save_snapshot` swallows all exceptions; token-event timestamp canonicalizer silently substitutes "now"
- **Where:** `src/gobby/storage/metric_snapshots.py:27-35`; `src/gobby/storage/token_events.py:49-55`
- **Note:** Metrics silently lost; malformed transcript timestamps re-date events to ingestion time. Log the fallback.

### [NIT] `get_context_window` prefix match treats stored model keys as LIKE patterns
- **Where:** `src/gobby/storage/model_costs.py:105-110` (`WHERE %s LIKE model || '%%'`)
- **Note:** `_` in a registry key wildcard-matches; wrong context window returned silently. Escape or match in Python.

### [NIT] Dead storage API surface: `list_pending_with_pid`, agent-run `delete`, clone `mark_cleanup`
- **Where:** `src/gobby/storage/agents/_runtime.py:159-165`; `agents/_queries.py:262-266`; `clones.py:357-367`
- **Note:** No non-test callers; `delete` would silently drop an active run. Remove or guard.

### [NIT] `last_activity_at` is allowlisted for `worktrees.update()` but the column does not exist
- **Where:** `src/gobby/storage/worktrees.py:270`; schema:779-794 has no such column
- **Note:** Any caller using the documented field gets UndefinedColumn at runtime — and it's the natural fix for the find_stale activity proxy. Add the column or drop the entry.

### [NIT] `list_conflicts` default `limit=100` silently truncates conflict hydration
- **Where:** `src/gobby/storage/merge_resolutions.py:516-555`; consumer `merge_conflict_hydration.py:86-89`
- **Note:** Resolutions with >100 conflicted files hydrate incompletely. Paginate or raise the limit explicitly.

### [NIT] `get_logger()` resolves the logger via `sys.modules` attribute sniffing per call
- **Where:** `src/gobby/storage/agents/_constants.py:31-38`
- **Note:** Test-seam hack on hot paths. Use the module logger; patch via caplog.

### [NIT] `create_memory` stores raw content but all dedup lookups compare normalized content
- **Where:** `src/gobby/storage/memories.py:153-156,190` vs `:278-302`
- **Note:** Whitespace-padded memories are invisible to `content_exists` while ID-dedup still fires. Insert the normalized form.

### [NIT] `update_access_stats` is a silent no-op for unknown memory IDs
- **Where:** `src/gobby/storage/memories.py:472-489`
- **Note:** No rowcount check, unlike sibling mutators. Check and log/raise.

### [NIT] `list_all_ids` paginates without ORDER BY
- **Where:** `src/gobby/storage/memories.py:385-396`
- **Note:** Paged scans can skip/duplicate. `ORDER BY id`.

### [NIT] `list_skills` dedups global-vs-project shadows after SQL LIMIT/OFFSET; `count_skills` disagrees
- **Where:** `src/gobby/storage/skills/_metadata.py:595-607` vs `:699-743`
- **Note:** Short pages, shifting offsets, irreconcilable list/count totals. Dedup in SQL (`DISTINCT ON (name)`).

### [NIT] `Skill.from_row` swallows metadata/allowed_tools JSON decode errors without logging
- **Where:** `src/gobby/storage/skills/_models.py:104-113`
- **Note:** Corrupt metadata → `None` → gobby-ownership detection (`skills/sync.py:74-79`) silently flips bundled rows to user-owned, stopping refreshes. Log with the skill id.

### [NIT] `set_skill_files` read-modify-write can PK-collide under concurrent sync
- **Where:** `src/gobby/storage/skills/_files.py:108-128`
- **Note:** Deterministic `skf-` ids + snapshot check; two syncs collide. `ON CONFLICT (skill_id, path) DO UPDATE`.

### [NIT] `WorkflowDefinitionRow.from_row` falsy-coalesces priority 0 to 100
- **Where:** `src/gobby/storage/workflow_definitions.py:85-92`
- **Note:** Highest-precedence rules display as 100; `""` → defaults likewise. Use `is None` checks.

### [NIT] `LocalWorkflowDefinitionManager.update` accepts arbitrary column names (no whitelist)
- **Where:** `src/gobby/storage/workflow_definitions.py:208-227` vs the whitelisted `projects.py:237-246`
- **Note:** Protected columns (`id`, `created_at`, `source`, `deleted_at`) freely mutable; unknown keys become opaque errors. Filter against an allowed set.

### [NIT] `prompts.list_prompts` category filter builds an unescaped LIKE pattern
- **Where:** `src/gobby/storage/prompts.py:547-550` (contrast `:585-595`, which escapes)
- **Note:** `_` in a category wildcard-matches. Reuse the search escaping.

### [NIT] Prompt `enabled` flag is never honored on the resolution path
- **Where:** `src/gobby/storage/prompts.py:340-381`; consumer `prompts/loader.py:79-102`
- **Note:** A disabled override would still shadow bundled. Add `AND enabled` or drop the column.

### [NIT] Audit helpers mutate the caller's context dict
- **Where:** `src/gobby/storage/workflow_audit.py:193-202`, `:264-270`
- **Note:** Reused dicts accumulate stale keys. `ctx = dict(context or {})`.

### [NIT] `create_plan` commits the registry row, then post-commit manifest generation can raise
- **Where:** `src/gobby/storage/plans.py:91-117`
- **Note:** Caller sees an exception; the plan row exists and is active. Validate `root_task_ref` before insert or document partial-success.

### [NIT] `resolve_execution_reference` prefix match returns an arbitrary row on ambiguity and breaks for NULL project scope
- **Where:** `src/gobby/storage/pipelines.py:518-548`; same NULL-scope inconsistency at `:728,746,907`
- **Note:** `gobby pipelines status <prefix>` can act on the wrong execution. Two-row fetch + ambiguity error; reuse the NULL-aware project clause.

### [NIT] Duplicated LIKE-escaping logic via `chr(92)` gymnastics in `count_search_executions`
- **Where:** `src/gobby/storage/pipelines.py:462-484` vs `:405-426`
- **Note:** Two hand-rolled copies that can drift. Extract one filter builder.

### [NIT] `CHAT_ATTACHMENTS_SCHEMA` is dead SQLite DDL drifted from the live Postgres schema
- **Where:** `src/gobby/storage/chat_attachments.py:13-64`; real DDL at schema:1648-1694
- **Note:** Already diverged (TEXT vs TIMESTAMPTZ). Delete the constant.

### [NIT] `cron_jobs.name` has no UNIQUE constraint but `get_job_by_name` is used as a bootstrap idempotency check
- **Where:** `src/gobby/storage/cron.py:223-233`; schema:972-992
- **Note:** Duplicate names → arbitrary-row lookups; system-job reconciliation can mutate the wrong one. Unique index + deterministic ordering.

### [NIT] `toggle_job` on system rows is two non-atomic updates
- **Where:** `src/gobby/storage/cron.py:568-579`
- **Note:** Failure between them parks an enabled job (`next_run_at=NULL`) invisibly. One transaction.

### [NIT] `record_delivery` conflates FK violations with webhook-dedup duplicates
- **Where:** `src/gobby/storage/github_triage.py:247-280` (`except (IntegrityError, UniqueViolation)`)
- **Note:** Unknown project_id reads as "already recorded", then a generic RuntimeError masks the cause. Catch only UniqueViolation.

### [NIT] Delivery-state managers silently drop unknown fields; ISM `get_messages` lacks ORDER BY; `auth.delete_session` always returns True; ISM dead dialect branch
- **Where:** `src/gobby/storage/delivery.py:215-231`; `inter_session_messages.py:224-233`, `:245`; `auth.py:71-74`; `sql_dialect.py:24-25`
- **Note:** Typo'd field names "succeed"; unordered reads; rowcount ignored; `is_postgres` hardcoded True leaves a dead branch. Contract hygiene.

## Systemic patterns

- **Lock-less read-modify-write is the default write idiom across every domain.** Tasks: claim/escalate (`_transitions.py:141-153,252-258`), labels/commits (`_lifecycle.py:104-214`), artifacts merge + attempt counter (`_artifacts.py:184-213,296-300`), description appends in submit/approve/reject (`_transitions.py:489-609`). Stage/dispatch: `update_run_context` (`build_history.py:159-173`), pristine-stage prune (`_build_cascade.py:147-210`). Sessions: lineage sanitize (`_lineage_guard.py:67-91`). Merge: `update_resolution`/`update_conflict`. Isolation: both `claim()`s. Comms: `mark_delivered`. Chat: seq allocation. Memory/skills/projects/prompts: SELECT-then-INSERT creates. Almost nothing uses `SELECT ... FOR UPDATE`, conditional UPDATE + rowcount, or `ON CONFLICT`. The correctly-built exceptions — `TaskSeqAllocation` (`_creation.py:48`), `github_triage.claim_delivery_for_processing` (`:289-315`), `chat_attachments.bind_attachments` (real LockTarget), agent terminal transitions (`agents/_lifecycle.py:188-321`) — prove the toolbox exists; it just isn't reached for.
- **Unguarded state transitions resurrect terminal states.** Agent `start()`, every expansion-run transition, pipeline approve-after-reject, cron toggle. Wherever a status predicate is missing from the UPDATE, a late writer can flip a cancelled/failed/completed row back to live.
- **Holder identity is never unique per logical acquisition** in the dispatch lease API: constant `"dispatcher"`, `"system"` fallback for session-less stage transitions. Acquire, release, and attach all key on this non-unique string — the root cause behind the lease Blocker and two Importants.
- **Worktrees and clones are diverging twins.** Each has fixes the other lacks: worktrees has the `status='merged'` find_expired filter and the get_by_task ORDER BY; clones has the unclaimed-only find_stale guard. Bugs get fixed in one copy and not ported; the duplication itself is the root cause (and produced this review's clone-reaper Blocker).
- **One logical operation, many transactions.** Reparent (edge then paths), cascade delete (per node), build cascade (flags then manifests), reopen/submit/approve (task txn + stage txn), toggle_job (two updates), audit rows written outside the mutating transaction (`_transitions.py:101-125,365-382,446-463`). Crashes leave committed intermediate states with no repair path or marker.
- **LIKE patterns from user fragments are never escaped, repo-wide.** tasks `_queries.py:199`/`_read.py:45,56`, sessions `session_resolution.py:126-141`, prompts `:547-550`, pipelines `:518-548`, model_costs `:105-110`. One shared `escape_like()` helper fixes all of them.
- **Truthiness guards and falsy coalescing destroy legitimate zero/empty values.** `if priority:` (`_queries.py:171,291`), priority-0→100 hydration (`workflow_definitions.py:85-92`), `{} and json.dumps(...)` (the stage-transition wedge), `json.dumps(x) if x else None` collapsing empty list/dict to NULL (memories, skills, workflow_definitions, prompts).
- **SQL LIMIT/OFFSET combined with post-SQL Python filtering/dedup/ordering** breaks pagination invariants: memory tag filters, skills shadow dedup vs count, task hierarchical ordering, the ready/blocked 1000-row cap, run-replay loops with no offset.
- **Deterministic content/name-derived IDs colliding with mutable rows**: uuid5(content) memories, `skl-`/`pmt-`/`skf-`/`mr-` hash ids — rename or edit leaves a row holding an id that a later create recomputes (raw PK violations), and the merge-resolution hash omitting `target_branch` breaks dedup-by-collision entirely.
- **`updated_at` as an activity proxy** for destructive sweeps (worktrees/clones staleness) when normal use never touches the row; agent runs solved this properly by joining session activity (`agents/_cleanup.py:46-55`).
- **Retention exists but is unscheduled or one-sided**: `workflow_audit.cleanup_old_entries` never called; comms retention deletes rows but leaks files while the chat-attachment twin returns records for unlinking.
- **Contract stubs standing in for behavioral tests**: `register_contract_tests` (`tests/phase2_stage_contract_helpers.py`) satisfies plan acceptance names with path/symbol assertions; the named mutex-serialization test asserts nothing about concurrency, and the destructive sweep queries have no behavioral tests at all.
- **Verified non-bugs worth recording:** `auth.py` token handling is sound (urandom(32), SHA-256 at rest, expiry enforced); `sql_dialect.py` does no regex SQL munging and validates identifiers; `github_triage` dedup/claim logic is correct; bundled-skill sync honors the enabled-toggle/user-row/orphan contract (`skills/sync.py:127-191,240-247`); alias-less `FROM (subquery)` is legal on the pinned Postgres 18; stage-state current-stage queries are deterministically ordered everywhere.
