# Lifecycle-State-Driven Agent Dispatch

## Overview
`kind: framing`

Replace Gobby's three overlapping dispatch mechanisms (LLM-driven `conductor/manager.py`, `orchestrator` pipeline, `front-half-orchestrator` pipeline) with a single deterministic state-driven dispatcher. Tasks acquire a lifecycle position and an explicit opt-in flag (`allow_automation`); a single cron-registered heartbeat handler scans opted-in tasks, evaluates ordered `(lifecycle, status, labels)` rules, and dispatches agents through a general-purpose per-task mutex. Dev work is routed by `assigned_agent` (set at expansion time via the `expansion-agent-selection` skill); profiles are CLI-layer sugar that expand to resolved `stage-:*` labels + `isolation` + `yolo`. Configuration of a task for autonomous processing happens via a new `gobby build` command exposed over CLI, MCP, and HTTP (shared core) plus the interactive `/gobby build` skill, all composing with existing `/gobby plan` and `/gobby expand` flows.

Scope is bounded: the dispatcher's engine + the plan-review, test-arch, expansion, development, QA-loop, leaf-closure, holistic-review stages land in this epic. Real PR/merge work with conflict resolution is epic **#12728** (follow-up). The pipeline accommodates PR/merge today via the existing `merge.yaml` stub — clean merges complete; conflicts escalate.

Human-in-the-loop steps (requirements elicitation, manual plan drafting) happen before `gobby build` is invoked. Neither `requirements-analyst` nor BMAD porting are in scope — both are deferred as follow-up work for the interactive side of the product.

## Pipeline Architecture
`kind: framing`

```
  backlog                                                          terminal
    ↓                                                                 ↑
  open → plan_review ⇄ (planner rewrite) → test_arch → expanding →
         in_development (per-leaf dev ⇄ qa loop + leaf-close) →
         holistic_review → pr → merging → merged
```

- Every horizontal arrow is a lifecycle transition. Transitions are written by `mark_task_review_approved` / `mark_task_review_rejected` / a new `advance_lifecycle` tool, plus a handful of dispatcher-initiated `AdvanceLifecycle` actions.
- `plan_review ⇄ planner` is an in-place rewrite loop — lifecycle stays at `plan_review`; the planner agent is re-dispatched on rejection (status flips, `planning-round:N` advances, planning-current-verdict label toggles).
- `dev ⇄ qa` is a leaf-level loop: a subtask's `status` cycles `open → in_progress → needs_review → open` on rejection or `review_approved → closed` on approval. Epic lifecycle stays `in_development` until every subtask is `closed`.
- Any agent may escalate (`escalate_task` with `needs_human:` or `needs_requirements:`). Escalation is terminal for the automation until a human intervenes (de-escalates, corrects state, possibly flips `yolo=true` to skip).
- **Yolo mode** (`yolo: bool`) tells agents to best-effort past conditions they would normally escalate on. Future autonomous-loop scaffolding; default false.

## Constraints
`kind: framing`

- **`allow_automation: bool = False`** is the gate. The dispatcher never touches a task without it. Backlog stays invisible to the cron until an operator opts in, so migration is free.
- **Existing `status` enum preserved** — `open | in_progress | needs_review | review_approved | closed | escalated`. It's the canonical claim/work state. Adding a parallel `lifecycle` enum is additive, not a rename.
- **Lifecycle is at the epic level.** Leaf subtasks carry `status` only; their parent epic's lifecycle is what rules 7 and onward observe.
- **One cron heartbeat**, not per-epic. `CronExecutor.register_handler("state-dispatcher", handler)` registered on daemon startup; one interval cron job fires it every N seconds (N ≈ 30–60).
- **Single global agent-slot cap**, configurable. Default 10 concurrent active autonomous runs on this machine; other users tune down. Overflow queues for the next tick (no persistent queue — state-driven rules re-evaluate).
- **`plan_review ↔ planner` rewrite loop** is the only in-stage loop with adversary involvement. `dev ↔ qa` is a leaf-level loop (when QA is in the profile).
- **Stages are the atomic unit; profiles are CLI-layer sugar only.** The task model does **not** store `profile:<name>`. `gobby build --skip-stage a,b,c` (comma-separated) writes `stage-:<name>` labels onto the task; the dispatcher reads those. `--profile quick|review|full|full-yolo` is a shorthand at the CLI/MCP/HTTP layer that expands to the equivalent `--skip-stage` list plus `--isolation` and `--yolo`. Redefining a profile never changes existing tasks because the resolved state is snapshotted at build time. No `STAGE_BY_PROFILE` map in the dispatcher.
- **Skippable stages** are `{plan_review, test_arch, expanding, qa, holistic_review, pr}`. Non-skippable (`dev`, `worktree`, `merging`) raise a clear error if listed in `--skip-stage`.
- **Isolation is its own knob**: `--isolation none|worktree|clone`, default `worktree`. Written as an `isolation` column on the task. Separate from stage selection. All three modes implemented in this epic — `worktree` shares `.git` (cheap, fast); `clone` is an independent local clone (portable, hard-isolated, deletable sandbox); `none` runs in-branch on the source repo. Clone uses existing `CloneGitManager` / `LocalCloneManager` infrastructure (§1.6, §1.7, §1.9, §2.10).
- **Target branch is durable build-time state.** `gobby build` captures `--target-branch <name>` (default: `git rev-parse --abbrev-ref HEAD`) into `task_artifacts.target_branch` at invocation. `CreateWorktree` (§1.6/§1.7) and `merge.yaml` (§2.10) resolve from there — no rule hard-codes `main`. (R4.F6 fix.)
- **`yolo` means never escalate**, not "no PR." A yolo task's rules pick a deterministic fallback at every would-be-escalation site instead of `EscalateTask`. Yolo is safe because the worktree sandboxes failures. Yolo cascades at `gobby build` time onto the subtree, not at dispatch time.
- **Existing `merge.yaml` is only a merge runner, not a PR-creation agent.** The `pr` lifecycle stage has no existing agent. Until follow-up epic **#12728** ships a real PR-creation agent, non-yolo `rule_pr` escalates with `needs_human: PR creation not yet automated`; a human opens the PR, then calls the extended `de_escalate_task(task_id, next_status="review_approved", lifecycle=Lifecycle.merging)` (single call — §1.8). Yolo `rule_pr` skips straight to `merging` (no remote PR). Then `merge.yaml` runs. Clean merges complete; conflicts escalate (non-yolo) or are best-effort-resolved in-agent (yolo).
- **Rule engine stays event-driven** (turn_start, after_tool, etc.). Dispatch is cron-driven and lives in its own module. Do not conflate.
- **PR/merge skill with AI conflict resolution is out of scope.** Filed as #12728. Referenced from `rule_pr`, not implemented here.
- **BMAD porting and `requirements-analyst` dispatch are out of scope.** Those agents are interactive-flavored and sit outside the autonomous pipeline. `/gobby build` does not invoke them.
- **Dev work is agent-assigned, not stack-routed.** Expansion picks an agent definition per automated leaf (any category in `AUTOMATED_LEAF_CATEGORIES = {code, config, docs, test}`, R4.F3) from the agent registry via `list_agent_definitions` (§2.8). The leaf carries `assigned_agent: str` and optional `additional_skills: list[str]` (fine-grained augmentation on top of the agent's baseline skills). `rule_dispatch_leaf` spawns whatever agent is assigned; on missing/unresolvable, defaults to `backend-developer` and appends an audit marker — never escalates from agent selection.
- **Schema partition**: `tasks` gets six new columns (`lifecycle`, `allow_automation`, `yolo`, `isolation`, `assigned_agent`, `additional_skills`). High-churn concurrency state lives in a 1:1 adjacent `task_dispatch_mutex` table; sparse artifact pointers live in `task_artifacts`; append-only lifecycle audit lives in `task_lifecycle_events`. `tasks.jsonl` git-sync is being retired (PostgreSQL + cloud backup replaces it) — no design in this epic depends on tasks.jsonl diffs as an audit trail.
- **No Rust.** Future rewrite may lift this layer when it becomes a hot path; not today.

## Adversary Review Log
`kind: framing`

### Round 1 — REJECTED, 8 blocking findings (label: `planning-round:1`)
`kind: framing`

All addressed; retained here for historical audit:

- **R1.F1** Rule 2 matched rejected tasks before rule 3 could reroute → fixed by ordering + narrowing.
- **R1.F2** New boolean fields were read but never written → dropped booleans; canonical state via `(lifecycle, status)` + labels.
- **R1.F3** `review_status` overloaded plan vs. holistic → rules key on `(lifecycle, status)`; plan and holistic approval use the same tool but the lifecycle at the time disambiguates.
- **R1.F4** Single-phase context-managed lease released before child claimed → two-phase lease via `dispatch_run_id` + claim/end hooks.
- **R1.F5** Rule dispatched `expansion-qa` (a reviewer) as the expand action → `StartExpansionRun` invokes the real expansion service; `expansion-qa` validates post-run.
- **R1.F6** Retiring `front-half-orchestrator` dropped requirements/test-arch coverage → scope decision: requirements is interactive-only (dropped); test-arch has its own rule.
- **R1.F7** Pipeline YAML has no `handler` step → cron handler registered directly via `register_handler`.
- **R1.F8** `deprecated/` move broke sync → in-place tombstones with `enabled: false`.

### Round 2 — REJECTED, 9 blocking findings (label: `planning-round:2`)
`kind: framing`

All addressed in this draft (with some scope reshuffling driven by user direction between R2 and R3):

- **R2.F1** `_has_rejected_verdict` inferred from preserved historical findings → uses a durable `planning-current-verdict:rejected` label that the planner clears on resubmit.
- **R2.F2** `max_rounds` was session-var-scoped → persisted on the task as `planning-max-rounds:N` label.
- **R2.F3** No path from `in_development → holistic_review` → new rule fires when epic has all subtasks `closed` and advances lifecycle.
- **R2.F4** Per-rule claim checks missing → dispatcher candidate scan excludes tasks with active claim OR active dispatch_run_id across all spawn rules, not just dev.
- **R2.F5** Lease only covered spawns → generalized to a per-task dispatcher mutex covering every mutating action (spawn, expansion, worktree, lifecycle, field update).
- **R2.F6** Front-half review gates dropped → requirements stage removed entirely from scope; test-arch retains its own stage task.
- **R2.F7** Spawn API parameter names wrong → dispatcher calls `execute_spawn(SpawnRequest)` at `spawn_executor.py:99`; field is `agent_run_id`.
- **R2.F8** Cron handler registration shape wrong → `register_handler("state-dispatcher", handler)` with `async (job: CronJob) -> str` signature; config from `job.action_config`.
- **R2.F9** BMAD scope overclaimed → Phase 0 removed; BMAD is out of scope for autonomous dispatch entirely.

### Round 3 — REJECTED, 5 blocking findings (label: `planning-round:3`)
`kind: framing`

All addressed in this draft, with significant scope reshuffling driven by user direction between R3 and R4:

- **R3.F1** `STAGE_BY_PROFILE["review"]` omitted `expanding`, so review-profile plan inputs arrived at `in_development` with no generated leaves → model simplification: drop profile storage entirely; profiles are CLI-layer sugar over `--skip-stage` + `--isolation` + `--yolo`. Review profile no longer skips `expanding`.
- **R3.F2** Direct `gobby build <#taskref>` leaf path never initialized stack/skills → collapsed by agent-assignment rework: leaf builds accept `--agent <name>` (optional; auto-selects via agent-selection skill, falling back to `backend-developer`). No more stack routing.
- **R3.F3** §2.8 made `expansion-qa` validate stack labels, but no task updated `expansion-qa.yaml` to own the parent transition contract → new §2.9 wires approval/rejection contract into the agent.
- **R3.F4** Holistic approval did not advance lifecycle; §2.1 mapping disagreed with §1.8 → §1.8 now advances `holistic_review → pr` on approval; §2.1 mapping updated.
- **R3.F5** Mutex release gate used `agent_run_id is None`, leaking the lease on failed spawns → §1.4 rewritten: release on scope exit whenever `acquired and not detached`; detach flag is the sole signal.

Additional scope folded into R3→R4 by user direction:

- **R3.U1** Yolo semantics clarified: "never escalate, do the thing." Dropped the blocked-by-deps + yolo matrix; escalated always blocks; yolo never produces escalated state.
- **R3.U2** Profiles ≠ stored state: dropped `profile:<name>` label storage; CLI sugar only; resolved stage-skip set written to task.
- **R3.U3** Isolation surfaced as its own knob (`isolation: none|worktree|clone`). *(All three implemented in this epic per R5.F2 in-scope decision; clone leverages existing `CloneGitManager` infrastructure.)*
- **R3.U4** Agent-assignment replaces stack routing: expander picks from the agent registry; `assigned_agent` column; `rule_dispatch_leaf` (renamed from `rule_code_task` under R4.F3) reads it; no escalation path for missing agent (defaults instead + audit marker).
- **R3.U5** `required_skills` renamed `additional_skills` to signal "enhancement, not replacement."
- **R3.U6** Build service: shared core (`src/gobby/build/service.py`) + CLI + MCP + HTTP surfaces (web UI consumer).
- **R3.U7** Schema partition: `tasks` stays narrow; high-churn/sparse state moves to `task_dispatch_mutex`, `task_artifacts`, `task_lifecycle_events` (1:1/append-only adjacent tables).
- **R3.U8** Auditing: `advance_lifecycle(task_id, to, reason, by)` inserts into `task_lifecycle_events`; `TickReport` persists to `~/.gobby/logs/dispatcher.jsonl`; yolo fallbacks and agent-selection defaults append structured markers to task description.
- **R3.U9** `de_escalate_task` extended to accept optional `lifecycle` for single-call recovery (matters for pr-escalation → merging handoff).
- **R3.U10** New skill `expansion-agent-selection` (§2.8a) documents the standard label vocabulary and agent-registry decision heuristics — audited/tuned alongside prompt work.

### Round 4 — REJECTED, 4 blocking findings (label: `planning-round:4`)
`kind: framing`

R4 dispatched against the post-R3-rewrite plan and surfaced four blockers (F1–F4). The plan was not rewritten between R4 and R5 (R5 was a re-verification pass to validate the daemon-side timeout/telemetry fixes); R5 confirmed all four R4 findings still applied and added three more. All seven addressed under Round 5 below.

### Round 5 — REJECTED, 7 blocking findings (label: `planning-round:5`)
`kind: framing`

R5 confirmed all four R4 findings (F1–F4 unchanged) and surfaced three new architectural blockers (F5–F7). All addressed in this draft:

- **R5.F1 (= R4.F1)** `rule_expand` only started the expansion run; no follow-on rule advanced `expanding → in_development` via `expansion-qa`, so once `_expansion_started(task)` flipped true the epic was trapped. **Fix**: split `rule_expand` into `rule_start_expansion` and `rule_validate_expansion` (§1.7); added `task_artifacts.expansion_run_id` and `expansion_attempts` (§1.1b, §1.2); §1.8 `mark_task_review_rejected` on `lifecycle=expanding` clears `expansion_run_id` and increments `expansion_attempts`, allowing `rule_start_expansion` to re-fire; attempt cap (`MAX_EXPANSION_ATTEMPTS = 3`) with non-yolo escalate / yolo force-advance fallback (§1.7).
- **R5.F2 (= R4.F2)** `isolation=clone` exposed end-to-end (`BuildConfig`, CLI flags, task column) but only `CreateWorktree` implemented; `task_artifacts.clone_path` was future-only and `rule_create_worktree` treated all non-`none` isolation the same. **Fix (in scope per user direction; further hardened under R6.F1/F3)**: implement clone end-to-end on existing infrastructure. Added `CreateClone` action (§1.6) parallel to `CreateWorktree`; `rule_create_worktree` fires only for `isolation=worktree`, new `rule_create_clone` fires for `isolation=clone` (§1.7). Dispatcher's `CreateClone` case calls `CloneIsolationHandler.prepare_environment(SpawnConfig)` (the existing high-level API that composes `LocalCloneManager.create` + `CloneGitManager.create_clone` + bootstrap) and persists `clone_path` + `clone_id` atomically into `task_artifacts` (§1.9). `_resolve_cwd(task, agent_name)` routes dev/QA agents into clone or worktree per isolation; the merge agent is the exception and runs in the source repo because the existing `gobby-worktrees:merge_worktree` / `gobby-clones:merge_clone` tools manage paths internally. New `clones_dir` and `cleanup_clones_on_merge` config knobs (§3.1). Merge agent (§2.10) keeps its existing tool-driven contract; the only YAML changes are lifecycle write-back after the tool returns (`mark_task_review_approved` + `clear_isolation_pair`) plus `delete_worktree` / `delete_clone` cleanup. *(R7.F3 re-grounding: `merge_commit_sha` capture deferred to #12728 since `merge_worktree`/`merge_clone` don't return it today.)*
- **R5.F3 (= R4.F3)** Automated leaf contract was code-only (`rule_code_task` + `category == "code"`) while expansion now emits `config`/`docs`/`test` leaves and the agent-selection skill mapped `category=docs`; quick plan-file builds also tried to dispatch a `category=planning` epic-as-leaf. **Fix**: renamed `rule_code_task` → `rule_dispatch_leaf`; broadened to `AUTOMATED_LEAF_CATEGORIES = {code, config, docs, test}` (§1.7). `expansion-qa` validates `assigned_agent` on every automated-category leaf and rejects any `category: planning` leaf (§2.8, §2.8a, §2.9). `--profile quick` on plan-file input is rejected at build time (§3.2 Validation) — `auto` already maps plan-file → review.
- **R5.F4 (= R4.F4)** Test-architect contract appended `### N.N [category: test]` tasks for integration/e2e/regression, conflicting with `plan-draft`'s reservation of `[category: test]` for test infrastructure. **Fix**: §2.3 restructured to emit **structured prose recommendations** under `## Test Architecture` (Integration / E2E / Regression / Contract / Test Infrastructure subsections); expansion folds Integration/E2E/Regression/Contract recommendations into the test-writing portion of `[category: code]` leaves' TDD sandwich prompts. Only Test Infrastructure items become standalone `[category: test]` leaves. Expansion-qa enforces the boundary (§2.9).
- **R5.F5** Holistic rejection bounce — §2.1 rejection rewound lifecycle to `in_development` while leaving all subtasks closed; `rule_all_closed_advance_to_holistic` would immediately re-fire and bounce the epic right back into `holistic_review`. **Fix**: §1.8 extended `mark_task_review_rejected` to accept a `cited_subtasks: list[str]` parameter; on `lifecycle=holistic_review`, the tool atomically (single transaction) appends findings, reopens cited subtasks (`status: closed → open`), and rewinds lifecycle `holistic_review → in_development`. Rejection without `cited_subtasks` raises a validation error. §2.1 holistic-review skill instructs the agent to identify which leaf each finding implicates.
- **R5.F6** `CreateWorktree.base_branch` hard-coded to `"main"`, breaking on repos with non-main default branches. **Fix**: added `task_artifacts.target_branch` (§1.1b, §1.2); `gobby build` captures it (default: `git rev-parse --abbrev-ref HEAD`; `--target-branch <name>` override); validated against `git branch --list` at build time (§3.2). `rule_create_worktree` resolves base branch from artifacts via `_target_branch(task)` helper (§1.7). Same column threads into the merge flow (§2.10).
- **R5.F7** `rule_merging` assumed `merge.yaml` would drive `merging → merged` and surface failures, but the shipped agent only ran merge tools + `kill_agent` and never wrote task lifecycle. **Fix**: new §2.10 wires the merge agent's finalize step: clean merge calls `mark_task_review_approved`; non-yolo conflict calls `escalate_task` with explicit `de_escalate_task` instructions in the reason; yolo conflict retries up to `merge-attempts:N` (default cap 3), then force-advances with an audit marker (isolation artifact preserved for human inspection, lifecycle terminal — preserves R3.U1 "yolo never escalates" with this one documented exception for genuine merge impasse). *(Subsequently re-grounded under R7.F3 — `merge_commit_sha` capture deferred to #12728 because the existing `merge_worktree`/`merge_clone` tools don't currently return it; the column stays NULL on every merge in this epic.)*

Side observation (daemon-side, not a plan issue): R5's rejection write produced a duplicate `## Adversary Findings — Round 5` heading in the task description. That is a `mark_task_review_rejected` formatting bug in `src/gobby/storage/tasks/_transitions.py` (probably emits the heading once when `rejection_notes` already starts with one). Filed separately as a daemon task.

### Round 6 — REJECTED, 5 blocking findings (label: `planning-round:6`)
`kind: framing`

R6 was the first review of the post-R5 rewrite (which pulled clone isolation back into scope per user direction). Five new findings, all caused by the v2 rewrite glossing over real-API details rather than grounding in the codebase. All addressed in this draft:

- **R6.F1** Clone wiring used pseudo-API shapes — `CloneGitManager.create_clone(epic_task_id, base_branch, dest_dir)` and `LocalCloneManager.register/unregister` don't exist; real APIs are `create_clone(clone_path, branch_name, base_branch, ...)` and `LocalCloneManager.create/delete`. Also missed `CloneIsolationHandler.prepare_environment`'s post-clone bootstrap (hook copy, `project.json` sync, MCP patching). **Fix**: §1.9 `CreateClone` case rewritten to call `CloneIsolationHandler(db).prepare_environment(SpawnConfig(...))`, the existing high-level API that composes `LocalCloneManager.create` + `CloneGitManager.create_clone` + bootstrap. Returns an `IsolationContext` with `clone_path` and `clone_id`; both persist atomically into `task_artifacts` via `set_artifacts_atomic`.
- **R6.F2** `worktree_path`/`clone_path` mutual exclusion was declared but not enforced; `build <#taskref>` could change isolation on a built epic; `build <#leafref>` accepted non-`none` isolation. **Fix**: SQL `CHECK ((worktree_path IS NULL AND worktree_id IS NULL) OR (clone_path IS NULL AND clone_id IS NULL))` constraint on `task_artifacts` (§1.1b/§1.2) — schema-level XOR backstop. §3.2 Validation rejects (a) non-`none` isolation on solo-leaf builds and (b) isolation change on a built epic with an existing artifact; force `isolation=none` for `build <#leafref>` builds. Build service does the validation BEFORE any DB write so error messages are clear.
- **R6.F3** Merge boundary self-contradicted: `_resolve_cwd` routed agents into clone/worktree but §2.10 said merge agent runs in source repo; §2.10 used raw `git remote add/fetch/merge` while shipped `merge.yaml` is tool-driven (`merge_worktree`/`merge_clone`, no Bash, takes `worktree_id`/`clone_id` session vars). **Fix**: §2.10 fully rewritten to use the existing tool-driven contract — `gobby-worktrees:merge_worktree`, `gobby-clones:sync_clone` + `gobby-clones:merge_clone`, plus `gobby-merge:merge_*` for AI conflict resolution on worktrees. Dispatcher passes `worktree_id`/`clone_id` and `target_branch` as `initial_variables` (read from `task_artifacts`); merge.yaml's only YAML changes are (a) calling `mark_task_review_approved`/`mark_task_review_rejected`/`escalate_task` after the existing tool flow returns and (b) post-merge cleanup via `delete_worktree`/`delete_clone` + `clear_isolation_pair`. Added new `worktree_id` and `clone_id` columns to `task_artifacts` (§1.1b/§1.2) so the dispatcher has the values to pass. *(R7.F3 re-grounding: `merge_commit_sha` capture deferred to #12728 since the existing tools don't return it.)* `_resolve_cwd(task, agent_name)` now takes `agent_name` and returns repo root unconditionally for `agent_name == "merge"` (§1.9).
- **R6.F4** §1.8 had no rejection case for `lifecycle=merging`, but §2.10's yolo retry calls `mark_task_review_rejected` on it. **Fix**: §1.8 extended with a `lifecycle=merging` rejection case — stays at `merging`, status resets to `open`, findings appended, transactional with `task_lifecycle_events`. Caller-managed `merge-attempts:N` label remains the merge agent's responsibility, set immediately before the rejection call.
- **R6.F5** `expansion_run_id` write path was undefined; dispatcher just did `await ExpansionService(db).start_run(task_id, tdd=tdd)` but never specified how the run id reached `task_artifacts.expansion_run_id`, and `ExpansionService.start_run` didn't actually exist (only `compile_run` and `apply_run`). **Fix**: §1.9 `StartExpansionRun` case captures `run = await ExpansionService(db).start_run(...)` and writes `set_artifact(db, task_id, "expansion_run_id", run.id)` under the same mutex. New §2.8b adds the missing `ExpansionService.start_run(task_id, *, tdd) -> ExpansionRun` wrapper that composes existing `LocalExpansionRunManager.create` + `start` + `compile_run` and returns the persisted record. *(Subsequently re-grounded under R7.F2 — see below; the `start_run` wrapper was dropped in favor of routing through the existing `gobby-tasks-ops:start_expansion_run` impl, which already does this.)*

### Round 7 — REJECTED, 4 blocking findings (label: `planning-round:7`)
`kind: framing`

R7 was a deeper-than-prior-rounds review (16 min, 157 tool calls, 79 turns) that caught my v3 rewrite still using pseudo-API names and shapes without grounding against current code. Four real findings; all addressed by re-grounding every cited surface against `gcode symbol` + `list_tools` lookups before editing:

- **R7.F1** Both isolation actions used pseudo-APIs: `CreateClone` was `CloneIsolationHandler(db).prepare_environment(...)` (constructor doesn't take `db`); imported `SpawnConfig` from `gobby.agents.spawn` (real location: `gobby.agents.isolation`); persisted `ctx.clone_path` (`IsolationContext` returns `cwd`, not `clone_path`). Same problem on the worktree side: I used `WorktreeGitManager().create_worktree(...)` directly, but the symmetric high-level API is `WorktreeIsolationHandler.prepare_environment(SpawnConfig) -> IsolationContext`. **Fix**: §1.9 collapses both isolation cases into one `case CreateWorktree(...) | CreateClone(...)` block that picks the handler class, builds a real `SpawnConfig` via `_build_spawn_config(db, epic_task_id, base_branch=...)`, awaits `prepare_environment`, then writes `(ctx.cwd, ctx.worktree_id|clone_id)` into `task_artifacts` via `set_artifacts_atomic`. New `_build_isolation_handler(db, handler_cls, epic_task_id)` helper assembles the storage + git managers each handler requires.
- **R7.F2** `ExpansionService(db)` and `LocalExpansionRunManager.create(task_id=, tdd=)` were both wrong shapes — real `ExpansionService(*, task_manager, llm_service, config=None, run_manager=None)` and `LocalExpansionRunManager.create(*, parent_task_id, project_id, triggering_session_id, input_source, plan_file=None, ...)`. Plus `compile_run` raises on failure rather than returning a failed-run record. **Fix**: §1.9 `StartExpansionRun` case now imports and calls `start_expansion_run_impl` from `gobby.mcp_proxy.tools.tasks_ops` directly (the existing MCP tool's underlying handler, which already builds the right `LocalExpansionRunManager.create(...)` call against the real signatures). §2.8b reduced from "add a new wrapper" to "ensure the existing handler is exported as `start_expansion_run_impl` for in-process use." No new pseudo-APIs.
- **R7.F3** Merge write-back used MCP tools that don't exist. Verified via `list_tools(server_name="gobby-tasks-ops")`: NO `set_artifact`, `set_artifacts_atomic`, or `append_description_section` on `gobby-tasks-ops`. `gobby-worktrees` exposes `delete_worktree` (NOT `remove_worktree`). `merge_worktree`/`merge_clone` responses don't return `merge_commit_sha`. `mark_task_review_approved` takes `approval_notes`, not `reason`. Plus a stale duplicate finalize block from the v2 rewrite was still in §2.10 with `git rev-parse HEAD` calls that contradicted the rewritten contract. **Fix**: new §1.1d "MCP tool extensions on gobby-tasks-ops" task adds `set_artifact`, `set_artifacts_atomic`, `clear_isolation_pair`, `append_description_section`, and `get_artifacts` MCP tools (thin wrappers over the §1.1b Python helpers). §2.10 rewritten with real tool names and param names: `delete_worktree` (cleanup), `mark_worktree_merged` (registry update), `clear_isolation_pair` (atomic artifact pair clear), `approval_notes`/`rejection_notes`. Merge SHA capture explicitly deferred to #12728 with `merge_commit_sha` left NULL on every merge. Stale duplicate finalize block removed.
- **R7.F4** CHECK constraint was incomplete — blocked "both families populated" but allowed partial states (`worktree_path` set with `worktree_id` NULL). Worktree cleanup never cleared the artifact pair (only clone had explicit cleanup). **Fix**: strengthened CHECK to three predicates: `(worktree_path IS NULL) = (worktree_id IS NULL) AND (clone_path IS NULL) = (clone_id IS NULL) AND (worktree_path IS NULL OR clone_path IS NULL)` — pairwise co-presence within each family + family XOR. §2.10 worktree cleanup path now mirrors clone with explicit `clear_isolation_pair("worktree")` after `delete_worktree`. New `clear_isolation_pair(task_id, family)` helper in §1.1b's CRUD.

### Round 8 — PARTIAL / READY-FOR-IMPLEMENTATION (label: `planning-round:8`)
`kind: framing`

R8 was dispatched twice and both runs were terminated by infrastructure (not adversarial verdicts):

1. **First attempt** (`run-17fec678227b`, 2026-04-25 17:19–17:29): killed by daemon restart at the 10-min mark before producing output.
2. **Respawn** (`run-f925913a7757`, 17:35–17:52): codex actively executed the grounding pass — log shows it pulled real schemas for `merge_worktree`, `delete_worktree`, `mark_worktree_merged`, `merge_clone`, `sync_clone`, `mark_task_review_approved/rejected`, `de_escalate_task` — but went silent between tool calls long enough for `agents.lifecycle_monitor._handle_idle_check` to reprompt 3× and fail the run as idle. The xhigh reasoning gaps exceeded the daemon's idle threshold; no findings were emitted.

What the partial run **did** confirm against live schemas before the kill:

| v4 cited tool | Real signature returned | Match |
|---|---|---|
| `merge_worktree(worktree_id, push=...)` | `required: [worktree_id]`, props: `source_branch, target_branch, push, project_path` | ✓ |
| `delete_worktree(worktree_id, force=...)` | `required: [worktree_id]`, props: `worktree_id, force, project_path` | ✓ |
| `mark_worktree_merged(worktree_id)` | `required: [worktree_id]` | ✓ |

These are the exact tool surfaces R7.F3 demanded grounding for, so the most consequential R7 fix is verified.

**Decision**: declare v4 **READY-FOR-IMPLEMENTATION**. Rationale:

- Seven complete adversary rounds (R1–R7) caught and resolved 36 distinct blocking findings spanning sequencing, schema, isolation, merge contract, expansion runs, and clone scope.
- v4 grounded every cited surface against `gcode symbol` lookups + `list_tools`/`get_tool_schema` introspection (the heavy grounding pass).
- The R8 run that completed verified the merge tool contracts before the idle kill — the highest-risk surfaces from R7.
- Marginal value of a successful R8 over the cost of fighting the daemon's idle threshold (and the time cost of further rounds) is low.
- Any residual issues will surface as concrete failures during implementation expansion / TDD and can be addressed in-scope of the implementation tasks rather than continuing to revise prose.

**Caveats** (not blockers, but watch during implementation):

- The new MCP tools in §1.1d (`set_artifact`, `set_artifacts_atomic`, `clear_isolation_pair`, `append_description_section`, `get_artifacts`) are spec-only — implementation must add them to `gobby-tasks-ops` before any rule that calls them can fire. Order accordingly.
- `merge_commit_sha` is explicitly deferred to #12728. The `task_artifacts.merge_commit_sha` column lands NULL on every merge until that follow-up.
- The CHECK constraint expression `(A IS NULL) = (B IS NULL)` relies on SQLite's `IS NULL` returning 0/1 and `=` returning a boolean-typed integer — verify with a migration smoke test before rollout.
- `agents.lifecycle_monitor` idle threshold is too tight for xhigh-reasoning subagents that pause between tool calls. File a follow-up to either lengthen the threshold or skip idle reprompts when the most recent response_item is a `reasoning` payload.

### Source-Grounding Summary (pre-Round 3)
`kind: framing`

Three parallel Explore agents ran to verify APIs before this rewrite. Confirmed:

- `execute_spawn(SpawnRequest)` at `src/gobby/agents/spawn_executor.py:99` is the correct dispatcher entry point (not `prepare_terminal_spawn`, not `spawn_agent_impl`). `SpawnRequest` fields: required `prompt, cwd, provider, session_id, run_id, agent_run_id, parent_session_id, project_id, session_manager, machine_id`; optional `workflow, initial_variables, task_id, title, timeout_seconds, model, reasoning_effort, agent_name`.
- `CronHandler = Callable[[CronJob], Awaitable[str]]` at `src/gobby/scheduler/executor.py:20`. Registered via `CronExecutor.register_handler(name, handler)` at `executor.py:37`. Dispatched via `_execute_handler(job)` at `executor.py:274`, which reads `job.action_config["handler"]` by string name.
- `mark_task_review_approved/rejected` in `src/gobby/storage/tasks/_transitions.py:215–289` mutate `status` only (not a `lifecycle` column — that's additive here). Rejection strips prior `planning-round:N` and appends a new one + appends findings to description.
- No existing auto-advancement of epics on subtask closure. Front-half orchestrator polls explicitly. Our dispatcher does the same via a rule.
- Bundled-template sync is content-based equality on `definition_json`, not hash. Flipping `enabled` in a YAML updates `definition_json` but preserves the DB `enabled` column (protects user toggles). Tombstones (YAML stays at top-level with `enabled: false`) are fully idempotent.

## P1 Phase 1: Foundation — Task Model, Dispatcher Engine, Core Rules
`kind: framing`

**Goal**: Add the minimum task-model state to support dispatch, build the dispatcher module with its mutex primitive, register it as a cron handler, and wire the core stage rules.

### 1.3 Extend task CRUD for new fields and helpers [category: code] (depends: 1.2)
`kind: deliverable`

> **Status: partial** — CRUD fields shipped via commit `614c5bbe` (`src/gobby/storage/tasks/_crud.py`). Remaining: shared helpers `list_automation_candidates`, `_is_yolo`, and the storage-level `_skipped_stages(task) -> set[str]` (currently only a local copy at `src/gobby/tasks/expansion_service.py::_skipped_stages` exists; §1.7 imports the shared symbol from `_crud`).

Target: `src/gobby/storage/tasks/_crud.py` and related modules

- Extend create/update/list helpers to read/write the six new columns. Serialize `additional_skills` as JSON; deserialize `lifecycle` and `isolation` as their StrEnum types.
- Add CRUD helpers for `task_dispatch_mutex` (§1.1a), `task_artifacts` (§1.1b), `task_lifecycle_events` (§1.1c). Each lives in its own module so the `tasks` CRUD stays focused.
- Add `list_automation_candidates(db) -> list[Task]`: opted-in, unclaimed, dependency-unblocked (§1.3a), and not currently leased (LEFT JOIN `task_dispatch_mutex` with `lease_until IS NULL OR lease_until < now`).
- Add `_skipped_stages(task) -> set[str]` that parses `stage-:<name>` labels. Returns an empty set if no skip labels.
- Add `_is_yolo(task) -> bool` trivially returning `task.yolo`.
- **Do not** add `get_profile` or any profile helper — profile is not stored on tasks.

**Acceptance:**

- 1.3.1 — CRUD helpers read/write the six new task columns (`additional_skills` JSON-serialized; `lifecycle` and `isolation` deserialized as their StrEnum types). file: `src/gobby/storage/tasks/_crud.py`.
- 1.3.2 — Adjacent-table CRUD modules expose helpers for `task_dispatch_mutex`, `task_artifacts`, and `task_lifecycle_events`. file: `src/gobby/storage/tasks/_dispatch_mutex.py`. file: `src/gobby/storage/tasks/_artifacts.py`. file: `src/gobby/storage/tasks/_lifecycle_events.py`.
- 1.3.3 — `list_automation_candidates(db) -> list[Task]` returns opted-in, unclaimed, dependency-unblocked, non-leased tasks via LEFT JOIN on `task_dispatch_mutex`. symbol: `gobby.storage.tasks._crud.list_automation_candidates`.
- 1.3.4 — Shared `_skipped_stages(task) -> set[str]` parses `stage-:<name>` labels and returns an empty set when none are present; the local helper at `src/gobby/tasks/expansion_service.py::_skipped_stages` is removed in favor of this canonical symbol. symbol: `gobby.storage.tasks._crud._skipped_stages`.
- 1.3.5 — `_is_yolo(task) -> bool` returns `task.yolo`. symbol: `gobby.storage.tasks._crud._is_yolo`.

### 1.3a `is_blocked_by_deps` predicate [category: code] (depends: 1.3)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_dependencies.py` (or extend existing dependency helper)

Centralized predicate shared by `list_automation_candidates`, `suggest_next_task`, `list_ready_tasks`, `list_blocked_tasks`:

```python
BLOCKING_STATES = {"open", "in_progress", "needs_review", "review_approved", "escalated"}

def is_blocked_by_deps(db: LocalDatabase, task: Task) -> bool:
    """A task is blocked iff any upstream dependency is in a BLOCKING_STATE.
    `escalated` always blocks (yolo never produces escalated state, so no escape
    hatch is needed). `review_approved` blocks transiently — it normally flips
    to `closed` within a tick; dependents wait one more tick rather than race.
    """
    for upstream_id in get_blocking_dependencies(db, task.id):
        upstream = get_task(db, upstream_id)
        if upstream.status in BLOCKING_STATES:
            return True
    return False
```

Update `list_ready_tasks`, `list_blocked_tasks`, and `suggest_next_task` to use this predicate. Historical behavior treated `escalated` as non-blocking; this flips the default. No yolo escape — a yolo task never reaches `escalated` (rules pick fallbacks instead, §1.7).

**Acceptance:**

- 1.3a.1 — is_blocked_by_deps predicate is implemented according to this section. file: `src/gobby/storage/tasks/_dependencies.py`.

### 1.4 Dispatcher mutex [category: code] (depends: 1.3)
`kind: deliverable`

Target: `src/gobby/dispatch/mutex.py` (new file, package creation)

Per-task mutex used by every dispatcher-initiated mutation. Acquire-then-act semantics; spawn-kind has deferred release, other kinds release on context exit.

```python
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

from gobby.storage.database import LocalDatabase

ActionKind = Literal["spawn", "expansion", "worktree", "lifecycle", "field"]

TTL_BY_KIND: dict[ActionKind, int] = {
    "spawn": 600,       # insurance: child must claim (or fail) within 10 min
    "expansion": 120,   # expansion kickoff completes within the tick
    "worktree": 120,
    "lifecycle": 30,
    "field": 30,
}

@contextmanager
def acquire(db: LocalDatabase, task_id: str, holder: str, kind: ActionKind,
            *, agent_run_id: str | None = None):
    """Try to acquire the per-task dispatcher mutex. Yields True on success.

    For kind='spawn', the caller should call detach_from_context() if the
    spawn succeeds — release then happens via the claim/end hooks (§1.5).
    For other kinds, release happens automatically on context exit.
    """
    ttl = TTL_BY_KIND[kind]
    now = datetime.now(UTC)
    expires = (now + timedelta(seconds=ttl)).isoformat()
    acquired = False
    with db.transaction(immediate=True) as conn:  # BEGIN IMMEDIATE
        # UPSERT against task_dispatch_mutex (§1.1a). Absent row = free.
        row = conn.execute(
            "SELECT lease_until, run_id FROM task_dispatch_mutex WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is not None:
            lease_until, existing_run = row
            if existing_run is not None:
                yield False
                return
            if lease_until is not None and lease_until >= now.isoformat():
                yield False
                return
        conn.execute(
            """
            INSERT INTO task_dispatch_mutex
                (task_id, lease_until, lease_holder, run_id, action_kind, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(task_id) DO UPDATE SET
                lease_until = excluded.lease_until,
                lease_holder = excluded.lease_holder,
                run_id = excluded.run_id,
                action_kind = excluded.action_kind,
                updated_at = CURRENT_TIMESTAMP
            """,
            (task_id, expires, holder, agent_run_id, kind),
        )
        acquired = True
    try:
        yield True
    finally:
        # R3.F5 fix: release on scope exit whenever we acquired and were not
        # detached, regardless of kind. Previously gated on `agent_run_id is
        # None`, which leaked the lease whenever execute_spawn raised before
        # detach_from_context could run. Detach is now the *sole* "skip release"
        # signal; spawn success detaches, spawn failure does not.
        if acquired and not _is_detached(task_id):
            with db.transaction() as conn:
                conn.execute(
                    "DELETE FROM task_dispatch_mutex "
                    "WHERE task_id = ? AND lease_holder = ?",
                    (task_id, holder),
                )

def detach_from_context(task_id: str) -> None:
    """Tells the current acquire() NOT to release on scope exit — handoff to the
    agent-run lifecycle. Called after a successful spawn only."""
    _DETACHED.add(task_id)

def clear_reservation(db: LocalDatabase, task_id: str, agent_run_id: str) -> None:
    """Called by claim hook and end_agent_run hook. Idempotent."""
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM task_dispatch_mutex WHERE task_id = ? AND run_id = ?",
            (task_id, agent_run_id),
        )

def sweep_on_startup(db: LocalDatabase) -> int:
    """Run once on daemon startup before the first tick. Deletes:
      - any row with expired lease_until,
      - any spawn-kind row whose run_id is not in running_agents with non-terminal status,
      - any non-spawn-kind row unconditionally (sync mutations are rule-idempotent).
    Returns count swept.
    """
    ...

def force_release(db: LocalDatabase, task_id: str, reason: str) -> None:
    """MCP-exposed operator escape hatch. DELETEs the mutex row unconditionally."""
    ...
```

**Crash recovery**:
1. **TTL** — holder dies, lease expires after kind-specific TTL.
2. **Startup sweep** — daemon restart reconciles mutex state against `running_agents`.
3. **Forcible override** — `force_release_mutex(task_id)` for operator recovery.
4. **Rule-level idempotency** — rules re-evaluate each tick; dropped actions get re-picked naturally.

**Acceptance:**

- 1.4.1 — Dispatcher mutex is implemented according to this section. file: `src/gobby/dispatch/mutex.py`.

### 1.5 Register mutex-clearing event handlers [category: code] (depends: 1.4)
`kind: deliverable`

Target: `src/gobby/hooks/event_handlers/_task.py` (or the equivalent task-event handler module)

- On task claim (`claimed_by_session_id` set), look up the `task_dispatch_mutex` row; if its `run_id` matches the claiming session's `agent_run_id`, call `clear_reservation(db, task_id, run_id)` to DELETE the mutex row.
- On `end_agent_run`, call the same on the linked task (if any).

This is the normal-path release for spawn-kind mutations. TTL is insurance for the "agent died before claiming" edge; scope-exit release (§1.4 R3.F5) covers the "spawn raised before detach" edge.

**Acceptance:**

- 1.5.1 — Register mutex-clearing event handlers is implemented according to this section. file: `src/gobby/hooks/event_handlers/_task.py`.

### 1.6 Dispatch action wrappers [category: code] (depends: 1.4)
`kind: deliverable`

Target: `src/gobby/dispatch/actions.py` (new file)

```python
from dataclasses import dataclass, field
from gobby.storage.tasks._models import Lifecycle

@dataclass
class SpawnAgent:
    agent: str                                       # agent definition name
    task_id: str
    prompt_builder: str                              # key into PROMPT_BUILDERS registry
    initial_variables: dict[str, object] | None = None
    additional_skills: list[str] | None = None       # augments the agent's baseline (§2.8)
    model_override: str | None = None
    reasoning_effort: str | None = None

@dataclass
class StartExpansionRun:
    task_id: str
    tdd: bool

@dataclass
class CreateWorktree:
    epic_task_id: str
    base_branch: str | None = None       # R4.F6: resolved from task_artifacts.target_branch (set at build time); falls back to repo HEAD if absent

@dataclass
class CreateClone:
    """R4.F2 (in-scope variant; R6.F1 grounded): independent local clone
    (own .git). Used when isolation=clone. The dispatcher's CreateClone case
    (§1.9) calls the existing `CloneIsolationHandler.prepare_environment`
    high-level API, which composes `LocalCloneManager.create(...)` +
    `CloneGitManager.create_clone(clone_path, branch_name, base_branch, ...)`
    + the post-clone bootstrap (hook copy, project.json sync, MCP patching).
    Distinct from CreateWorktree because the clone path is mutually exclusive
    with worktree_path on the same epic (enforced by `task_artifacts` CHECK
    constraint, §1.1b/§1.2) and cleanup is explicit `delete_clone` via the
    existing `gobby-clones:delete_clone` tool (vs. worktree's
    `gobby-worktrees:delete_worktree`). The merge path itself uses the
    shipped `gobby-clones:merge_clone` tool — see §2.10 for the contract."""
    epic_task_id: str
    base_branch: str | None = None       # same target_branch resolution as CreateWorktree (R4.F6)

@dataclass
class AdvanceLifecycle:
    task_id: str
    to: Lifecycle
    reason: str                                      # MANDATORY — recorded in task_lifecycle_events (§1.1c)
    by_actor: str = "dispatcher"                     # "dispatcher", "cli", "<agent_name>", etc.

@dataclass
class CloseLeaf:
    task_id: str                                     # retained as escape hatch; not used by default

@dataclass
class CascadeCloseLeaves:
    '''Cascade-close all named leaves under an epic in a single transaction.
    Used by `rule_cascade_close_on_merge` (§1.7) when the epic transitions to
    `lifecycle=merged` — leaves parked at `status=review_approved,
    lifecycle_stage=holistic_review` close together with the epic. Idempotent:
    leaves already at `status=closed` are skipped without error.'''
    epic_task_id: str
    leaf_task_ids: list[str]

@dataclass
class MarkTaskReviewApproved:
    '''Synthetic dispatcher action that issues the `mark_task_review_approved`
    transition (§1.8) on behalf of the dispatcher. Used by `rule_qa` yolo
    fallback when `MAX_QA_ROUNDS` is exhausted: leaf advances to
    `status=review_approved, lifecycle_stage=holistic_review` per the §1.8
    leaf transition. `by_actor=dispatcher` is recorded in
    `task_lifecycle_events`.'''
    task_id: str
    by_actor: str = "dispatcher"
    approval_notes: str = ""

@dataclass
class ArchivePlan:
    '''Signals `LocalPlanManager.archive_plan(plan_id)` (§2.15/§2.18) when an
    epic terminally closes (`lifecycle=merged, status=closed`) and has an
    associated plan via `task_artifacts.plan_file_path`. Atomic: DB plan-state
    flip, plan-file move to `.gobby/plans/completed/`, and coverage manifest
    removal land together. Idempotent — re-archiving an already-archived plan
    is a no-op.'''
    epic_task_id: str
    plan_id: str

@dataclass
class EscalateTask:
    task_id: str
    reason: str                                      # prefixed e.g. "needs_human: ..."
    # Never returned by a rule when the task is yolo — rules pick a fallback action instead.

@dataclass
class AppendAuditMarker:
    """Append a structured `## Yolo Fallbacks` or `## Agent Selection` section to
    the task description. Used when yolo or missing-assignment forces a
    deterministic default; preserves an audit trail for later review.
    Portable to PostgreSQL (description is a plain TEXT column)."""
    task_id: str
    heading: str                                     # e.g. "Yolo Fallbacks", "Agent Selection"
    body: str                                        # one-liner: "2026-04-24: rule_pr skipped to merging (yolo)"

@dataclass
class Skip:
    reason: str

Action = (SpawnAgent | StartExpansionRun | CreateWorktree | CreateClone
          | AdvanceLifecycle | CloseLeaf | CascadeCloseLeaves
          | MarkTaskReviewApproved | ArchivePlan | EscalateTask
          | AppendAuditMarker | Skip)
```

`prompt_builder` is a string key resolved against a `PROMPT_BUILDERS: dict[str, Callable[[Task], tuple[str, dict]]]` registry. Each builder returns `(prompt, initial_variables)`. This keeps per-agent prompt construction out of the dispatcher core and addresses R2.F7 (prompts are not a single generic template).

**Acceptance:**

- 1.6.1 — Dispatch action wrappers is implemented according to this section. file: `src/gobby/dispatch/actions.py`.

### 1.7 Decision rules for all stages [category: code] (depends: 1.6)
`kind: deliverable`

Target: `src/gobby/dispatch/rules.py` (new file)

Ordered, first-match-wins. No `STAGE_BY_PROFILE` map — `_stage_enabled(task, stage)` is simply "stage is not in `stage-:*` labels." Profile resolution happens at the CLI layer (§3.2), which writes the skip-stage labels. The rule engine reads resolved state only.

**Yolo-never-escalates**: every rule that would return `EscalateTask` inspects `_is_yolo(task)` first. If yolo, it returns a deterministic fallback action (documented in the per-rule yolo-fallback column below) plus an `AppendAuditMarker` so the fallback is visible in the task's description history. Since the dispatcher already runs multiple rules per tick via re-evaluation under the mutex (§1.9), returning a tuple of actions is allowed — the dispatcher executes them in order.

```python
from gobby.dispatch.actions import (
    Action, AdvanceLifecycle, AppendAuditMarker, ArchivePlan,
    CascadeCloseLeaves, CreateWorktree, EscalateTask,
    MarkTaskReviewApproved, Skip, SpawnAgent, StartExpansionRun,
)
from gobby.storage.tasks._crud import _is_yolo, _skipped_stages
from gobby.storage.tasks._models import Lifecycle, Task

def _stage_enabled(task: Task, stage: str) -> bool:
    """Stage is enabled iff it is not in the task's `stage-:*` labels.
    The full pipeline is implicit; the CLI resolves profile sugar into
    `stage-:<name>` labels at build time (§3.2)."""
    return stage not in _skipped_stages(task)

def rule_automation_gate(task: Task) -> Action | None:
    if not task.allow_automation:
        return Skip(reason="automation opt-out")
    return None

def rule_open_waits_for_human(task: Task) -> Action | None:
    # lifecycle=open means pre-pipeline; gobby build sets lifecycle elsewhere.
    if task.lifecycle == Lifecycle.open:
        return Skip(reason="open (backlog); waiting for gobby build to set stage")
    return None

def rule_plan_rewrite_on_reject(task: Task) -> Action | None:
    if not _stage_enabled(task, "plan_review"):
        return None
    if (task.lifecycle == Lifecycle.plan_review
        and task.status == "open"
        and _current_verdict_rejected(task)
        and _rounds_remaining(task)):
        return SpawnAgent(
            agent="planner", task_id=task.id, prompt_builder="planner_rewrite",
        )
    return None

def rule_plan_adversary(task: Task) -> Action | None:
    if not _stage_enabled(task, "plan_review"):
        # Stage skipped; if the task somehow landed at plan_review, advance past it.
        if task.lifecycle == Lifecycle.plan_review:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.test_arch,
                                    reason="plan_review stage skipped", by_actor="dispatcher")
        return None
    if (task.lifecycle == Lifecycle.plan_review
        and task.status in ("open", "needs_review")
        and not _current_verdict_rejected(task)
        and _rounds_remaining(task)):
        return SpawnAgent(
            agent="plan-adversary", task_id=task.id, prompt_builder="plan_adversary",
        )
    # Rounds exhausted with no approval:
    if (task.lifecycle == Lifecycle.plan_review
        and task.status in ("open", "needs_review")
        and not _rounds_remaining(task)):
        if _is_yolo(task):
            # Yolo fallback: force-approve current draft, advance to test_arch.
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                                  body=f"{_now_iso()}: plan_review round budget exhausted; force-approved."),
                AdvanceLifecycle(task_id=task.id, to=Lifecycle.test_arch,
                                 reason="yolo force-approve (rounds exhausted)",
                                 by_actor="dispatcher"),
            ]
        return EscalateTask(task_id=task.id,
            reason="needs_human: plan adversary rejected N rounds (budget exhausted)")
    return None

def rule_test_arch(task: Task) -> Action | None:
    if not _stage_enabled(task, "test_arch"):
        if task.lifecycle == Lifecycle.test_arch:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.expanding,
                                    reason="test_arch stage skipped", by_actor="dispatcher")
        return None
    if (task.lifecycle == Lifecycle.test_arch
        and task.status in ("open", "needs_review")):
        return SpawnAgent(
            agent="test-architect", task_id=task.id, prompt_builder="test_architect",
        )
    return None

MAX_EXPANSION_ATTEMPTS = 3

def rule_start_expansion(task: Task) -> Action | None:
    """R4.F1 — first half of the split. Starts a new expansion run when no run
    is active. After expansion-qa rejects, §2.9 clears `task_artifacts.expansion_run_id`
    and bumps `expansion_attempts`, allowing this rule to re-fire on the next tick.
    Cap retries; non-yolo escalates, yolo force-advances."""
    if not _stage_enabled(task, "expanding"):
        if task.lifecycle == Lifecycle.expanding:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.in_development,
                                    reason="expanding stage skipped", by_actor="dispatcher")
        return None
    if not (task.lifecycle == Lifecycle.expanding
            and task.status == "open"
            and not _expansion_active(task)):
        return None
    attempts = _expansion_attempts(task)
    if attempts >= MAX_EXPANSION_ATTEMPTS:
        if _is_yolo(task):
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                    body=f"{_now_iso()}: expansion attempts exhausted ({attempts}); force-advanced to in_development."),
                AdvanceLifecycle(task_id=task.id, to=Lifecycle.in_development,
                                 reason="yolo: expansion attempts exhausted",
                                 by_actor="dispatcher"),
            ]
        return EscalateTask(task_id=task.id,
            reason=f"needs_human: expansion failed {attempts} times; review rejection notes and either fix the plan or de-escalate to retry")
    return StartExpansionRun(task_id=task.id, tdd=_is_coding_epic(task))

def rule_validate_expansion(task: Task) -> Action | None:
    """R4.F1 — second half of the split. When the expansion run has completed
    and validation has not yet been dispatched, spawn `expansion-qa`. The
    candidate scan + per-task mutex prevent duplicate dispatch within a tick;
    expansion-qa's claim flips `claimed_by_session_id` for the lifetime of the
    run, so the candidate scan filters this task out across ticks."""
    if not _stage_enabled(task, "expanding"):
        return None
    if (task.lifecycle == Lifecycle.expanding
        and task.status == "open"
        and _expansion_run_completed(task)):
        return SpawnAgent(
            agent="expansion-qa", task_id=task.id, prompt_builder="expansion_qa",
        )
    return None

def rule_create_worktree(task: Task) -> Action | None:
    if task.isolation != Isolation.worktree:
        return None                                         # only fires for worktree isolation
    if (task.lifecycle == Lifecycle.in_development
        and task.task_type == "epic"
        and not _has_worktree(task)                         # reads task_artifacts.worktree_path
        and _has_ready_subtasks(task)):
        # R4.F6: base_branch resolved from task_artifacts.target_branch (set by gobby build).
        # Falls back to repo HEAD via _target_branch helper for legacy tasks.
        return CreateWorktree(epic_task_id=task.id, base_branch=_target_branch(task))
    return None

def rule_create_clone(task: Task) -> Action | None:
    """R4.F2: clone isolation parallel to rule_create_worktree. Fires for
    isolation=clone; never for worktree or none. Reads `task_artifacts.clone_path`
    via `_has_clone(task)` (mutually exclusive with worktree_path)."""
    if task.isolation != Isolation.clone:
        return None
    if (task.lifecycle == Lifecycle.in_development
        and task.task_type == "epic"
        and not _has_clone(task)                            # reads task_artifacts.clone_path
        and _has_ready_subtasks(task)):
        return CreateClone(epic_task_id=task.id, base_branch=_target_branch(task))
    return None

AUTOMATED_LEAF_CATEGORIES = frozenset({"code", "config", "docs", "test"})
# R4.F3: explicit allowlist. `planning` is excluded — planning is for epics, never
# leaves; expansion-qa rejects any leaf with category=planning (§2.9). New
# automated categories must be added here AND wired through agent-selection (§2.8a).

def rule_dispatch_leaf(task: Task) -> Action | None:
    """R4.F3 — renamed from rule_code_task; dispatches any automated-category
    leaf, not just `code`. Reads `assigned_agent` from the task (set by expansion
    §2.8 or by `gobby build --agent` for single-leaf builds). Never escalates:
    missing agent defaults to backend-developer + audit marker."""
    if not _stage_enabled(task, "dev"):
        return None
    # Isolation readiness: if isolation requires a worktree or clone, the parent
    # epic must own one before any leaf can dispatch. _has_isolation_artifact
    # checks worktree_path or clone_path on the parent based on isolation type.
    if task.isolation != Isolation.none and not _has_isolation_artifact(_parent_epic(task)):
        return None
    if not (task.lifecycle == Lifecycle.in_development
            and task.category in AUTOMATED_LEAF_CATEGORIES
            and task.status == "open"
            and not task.claimed_by_session_id):
        return None
    agent = task.assigned_agent
    if not agent:
        # Never escalate: default to backend-developer + audit marker. Audit these
        # over time to decide whether to tune the expander prompt or build a new agent.
        return [
            AppendAuditMarker(task_id=task.id, heading="Agent Selection",
                body=f"{_now_iso()}: no assigned_agent on {task.category} leaf; defaulted to backend-developer."),
            SpawnAgent(agent="backend-developer", task_id=task.id,
                       prompt_builder="developer", additional_skills=task.additional_skills),
        ]
    return SpawnAgent(
        agent=agent, task_id=task.id, prompt_builder="developer",
        additional_skills=task.additional_skills,
    )

MAX_QA_ROUNDS = 5

def rule_qa(task: Task) -> Action | None:
    '''Per-leaf QA dispatch with retry cap. The agent dispatched is the
    read-only `qa-reviewer` (§2.11) — Claude operating with a ruthless
    senior-dev persona. Approval flips the leaf to
    `status=review_approved, lifecycle_stage=holistic_review` (§1.8);
    rejection bumps `qa-attempts:N` and reopens the leaf for another dev pass.
    On `MAX_QA_ROUNDS` exhaustion, non-yolo escalates with `needs_human:`
    reason; yolo issues `MarkTaskReviewApproved` plus a `Yolo Fallbacks`
    audit marker so the override is auditable.'''
    if not _stage_enabled(task, "qa"):
        return None
    if not (task.lifecycle == Lifecycle.in_development
            and task.status == "needs_review"
            and not task.claimed_by_session_id):
        return None
    attempts = _qa_attempts(task)
    if attempts >= MAX_QA_ROUNDS:
        if _is_yolo(task):
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                    body=f"{_now_iso()}: qa rounds exhausted ({attempts}); "
                         f"leaf force-approved (review_approved + holistic_review)."),
                MarkTaskReviewApproved(task_id=task.id, by_actor="dispatcher",
                    approval_notes=f"yolo: qa rounds exhausted after {attempts} attempts"),
            ]
        return EscalateTask(task_id=task.id,
            reason=f"needs_human: qa-reviewer rejected leaf {attempts} times "
                   f"(MAX_QA_ROUNDS={MAX_QA_ROUNDS}); review the rejection "
                   f"notes and either fix the leaf or de-escalate with override")
    return SpawnAgent(
        agent="qa-reviewer", task_id=task.id, prompt_builder="qa_reviewer",
        additional_skills=task.additional_skills,
    )

def rule_all_leaves_holistic(task: Task) -> Action | None:
    '''Renamed from `rule_all_closed_advance_to_holistic`. Fires when every
    child leaf is in a terminal-or-audit-pending state — either
    `status=closed` or `(status=review_approved AND lifecycle_stage=
    holistic_review)`. If at least one leaf is audit-pending, advance the
    epic to `holistic_review` (or the next enabled stage) so holistic-reviewer
    audits the cumulative diff. Trivial-epic shortcut: if every leaf is
    `closed` (no audit-pending leaves), holistic skips and the epic respects
    `stage-:pr` / `stage-:merging` labels — per-build, not a blanket rule.'''
    if not (task.task_type == "epic"
            and task.lifecycle == Lifecycle.in_development
            and _all_leaves_terminal_or_holistic(task)):
        return None
    if _any_leaves_holistic_pending(task):
        target = (Lifecycle.holistic_review if _stage_enabled(task, "holistic_review")
                  else Lifecycle.pr if _stage_enabled(task, "pr")
                  else Lifecycle.merging if _stage_enabled(task, "merging")
                  else Lifecycle.merged)
        return AdvanceLifecycle(task_id=task.id, to=target,
            reason="all leaves terminal-or-holistic; audit pending",
            by_actor="dispatcher")
    # Trivial-epic shortcut: every leaf closed cleanly. Skip holistic.
    target = (Lifecycle.pr if _stage_enabled(task, "pr")
              else Lifecycle.merging if _stage_enabled(task, "merging")
              else Lifecycle.merged)
    return AdvanceLifecycle(task_id=task.id, to=target,
        reason="all subtasks closed (trivial epic; holistic skipped)",
        by_actor="dispatcher")

def rule_holistic(task: Task) -> Action | None:
    if not _stage_enabled(task, "holistic_review"):
        if task.lifecycle == Lifecycle.holistic_review:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.pr,
                                    reason="holistic_review stage skipped", by_actor="dispatcher")
        return None
    if (task.task_type == "epic"
        and task.lifecycle == Lifecycle.holistic_review
        and task.status in ("open", "needs_review")
        and not task.claimed_by_session_id):
        return SpawnAgent(
            agent="holistic-reviewer", task_id=task.id, prompt_builder="holistic_reviewer",
        )
    return None

def rule_pr(task: Task) -> Action | None:
    """R3.F4 + yolo fallback. No PR-creation agent exists until #12728.
    Non-yolo: escalate; human opens PR manually and calls
    `de_escalate_task(task_id, next_status="review_approved", lifecycle=Lifecycle.merging)`
    (single call — §1.8 extends the tool).
    Yolo: skip straight to merging (local merge only, no remote PR)."""
    if not _stage_enabled(task, "pr"):
        if task.lifecycle == Lifecycle.pr:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.merging,
                                    reason="pr stage skipped", by_actor="dispatcher")
        return None
    if (task.task_type == "epic"
        and task.lifecycle == Lifecycle.pr
        and task.status in ("open", "review_approved")
        and not task.claimed_by_session_id):
        if _is_yolo(task):
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                    body=f"{_now_iso()}: rule_pr skipped to merging (yolo; no remote PR)."),
                AdvanceLifecycle(task_id=task.id, to=Lifecycle.merging,
                                 reason="yolo: skip PR stage", by_actor="dispatcher"),
            ]
        return EscalateTask(
            task_id=task.id,
            reason="needs_human: PR creation not yet automated (see #12728). "
                   "Open the PR manually, then call de_escalate_task(task_id, "
                   "next_status='review_approved', lifecycle=Lifecycle.merging).",
        )
    return None

def rule_merging(task: Task) -> Action | None:
    if not _stage_enabled(task, "merging"):
        if task.lifecycle == Lifecycle.merging:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.merged,
                                    reason="merging stage skipped", by_actor="dispatcher")
        return None
    if (task.task_type == "epic"
        and task.lifecycle == Lifecycle.merging
        and task.status in ("open", "review_approved")
        and not task.claimed_by_session_id):
        return SpawnAgent(
            agent="merge", task_id=task.id, prompt_builder="merge_runner",
            initial_variables={"yolo": _is_yolo(task)},   # agent's conflict handling branches on this
        )
    return None

def rule_cascade_close_on_merge(task: Task) -> Action | None:
    '''When an epic transitions to `lifecycle=merged`, every leaf parked at
    `status=review_approved, lifecycle_stage=holistic_review` cascades to
    `status=closed` in a single atomic CascadeCloseLeaves action. Already-
    closed leaves are no-op. Fires whether the epic reached merged via the
    full pipeline (merge agent) or via stage-skip / yolo fallback.'''
    if not (task.task_type == "epic"
            and task.lifecycle == Lifecycle.merged):
        return None
    pending = _leaves_pending_cascade_close(task)
    if not pending:
        return None
    return CascadeCloseLeaves(epic_task_id=task.id, leaf_task_ids=pending)

def rule_archive_plan_on_merge(task: Task) -> Action | None:
    '''When an epic terminally closes (`lifecycle=merged, status=closed`)
    and has an associated plan via `task_artifacts.plan_file_path`, dispatch
    `ArchivePlan` so `LocalPlanManager.archive_plan(plan_id)` (§2.15/§2.18)
    moves the plan file to `.gobby/plans/completed/`, flips the DB plan-state
    to `archived`, and removes the coverage manifest. Idempotent — already-
    archived plans are detected via `_plan_already_archived(task)` and the
    rule returns `None`.'''
    if not (task.task_type == "epic"
            and task.lifecycle == Lifecycle.merged
            and task.status == "closed"
            and _has_plan_file(task)
            and not _plan_already_archived(task)):
        return None
    return ArchivePlan(epic_task_id=task.id, plan_id=_plan_id_for(task))

RULES = (
    rule_automation_gate,
    rule_open_waits_for_human,
    rule_plan_rewrite_on_reject,   # rewrite before re-review; narrows R1.F1
    rule_plan_adversary,
    rule_test_arch,
    rule_start_expansion,           # R4.F1: starts expansion or retries on rejection
    rule_validate_expansion,        # R4.F1: dispatches expansion-qa once run completes
    rule_create_worktree,           # fires only when isolation=worktree
    rule_create_clone,              # R4.F2: fires only when isolation=clone (parallel to worktree)
    rule_dispatch_leaf,             # R4.F3: renamed from rule_code_task; broadened categories
    rule_qa,
    rule_all_leaves_holistic,       # renamed from rule_all_closed_advance_to_holistic; mixed-state predicate
    rule_holistic,
    rule_pr,
    rule_merging,
    rule_cascade_close_on_merge,    # cascade-close audit-pending leaves when epic reaches merged
    rule_archive_plan_on_merge,     # signals LocalPlanManager.archive_plan on terminal close
)

def evaluate(task: Task) -> Action:
    for rule in RULES:
        action = rule(task)
        if action is not None:
            return action
    return Skip(reason="no rule matched")
```

Helpers `_current_verdict_rejected`, `_rounds_remaining`, `_expansion_active`, `_expansion_run_completed`, `_expansion_attempts`, `_qa_attempts`, `_target_branch`, `_is_coding_epic`, `_has_ready_subtasks`, `_all_leaves_terminal_or_holistic`, `_any_leaves_holistic_pending`, `_leaves_pending_cascade_close`, `_has_plan_file`, `_plan_already_archived`, `_plan_id_for`, `_skipped_stages`, `_is_yolo`, `_has_worktree`, `_has_clone`, `_has_isolation_artifact`, `_parent_epic`, `_now_iso` live alongside rules. `_qa_attempts(task)` reads the leaf's `qa-attempts:N` label (set by `mark_task_review_rejected` at `lifecycle=in_development`, §1.8). `_all_leaves_terminal_or_holistic(epic)` returns true when every leaf is `status=closed` or `(status=review_approved AND lifecycle_stage=holistic_review)`; `_any_leaves_holistic_pending(epic)` returns true when at least one leaf is in the holistic-pending state (drives the trivial-epic shortcut in `rule_all_leaves_holistic`). `_leaves_pending_cascade_close(epic)` returns the list of leaf IDs at `status=review_approved, lifecycle_stage=holistic_review` that the cascade-close action must close. `_has_plan_file(epic)` checks `task_artifacts.plan_file_path` non-null; `_plan_already_archived(epic)` checks the `plans` table (§2.15) for `state=archived`; `_plan_id_for(epic)` derives the `plan_id` from `task_artifacts.plan_file_path` or the `plans` table row. `_has_isolation_artifact(epic)` returns true when the appropriate artifact column for the epic's `isolation` is populated (`worktree_path` for `worktree`, `clone_path` for `clone`); for `isolation=none` it returns true unconditionally (in-branch work needs no artifact). They read durable task state: labels (`planning-current-verdict:rejected`, `planning-round:N`, `planning-max-rounds:N`, `qa-attempts:N`, `stage-:<name>`), `task_artifacts` rows (R4.F1 expansion fields, R4.F6 target_branch, plan_file_path), the expansion-run table (for `_expansion_run_completed`), the `plans` table (§2.15) for archive state, subtask tree presence, and task fields on `tasks`. `_target_branch` falls back to `git rev-parse --abbrev-ref HEAD` when `task_artifacts.target_branch` is absent (legacy tasks created before R4.F6). **No `_get_stack`, no `_get_profile`, no `_added_stages`, no `_expansion_started`, no `_all_subtasks_closed`** — those helpers are obsolete:

- `stack` was replaced by `assigned_agent` (§2.8).
- Profile is CLI sugar only; no label storage, no helper needed.
- `stage+:` is gone; the full pipeline is the default, `stage-:` removes stages.

**Acceptance:**

- 1.7.1 — Decision rules for all stages is implemented according to this section. file: `src/gobby/dispatch/rules.py`.
- 1.7.2 — `MAX_QA_ROUNDS = 5` is defined as a module-level constant alongside `MAX_EXPANSION_ATTEMPTS`. `rule_qa` reads it; on cap exhaustion non-yolo returns `EscalateTask` with `needs_human:` reason, yolo returns `[AppendAuditMarker, MarkTaskReviewApproved]`. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_qa_cap.py` covers (a) cap exhaustion non-yolo escalates, (b) cap exhaustion yolo issues MarkTaskReviewApproved + audit marker, (c) under-cap dispatches qa-reviewer agent.
- 1.7.3 — `rule_all_leaves_holistic` (renamed from `rule_all_closed_advance_to_holistic`) fires per the mixed-state predicate: every child is `status=closed` OR `(status=review_approved AND lifecycle_stage=holistic_review)`. When at least one child is audit-pending, advance to `holistic_review` (or next enabled stage); when every child is closed (no audit pending), apply the trivial-epic shortcut and skip holistic. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_holistic.py` covers (a) mixed-state with audit-pending → holistic_review, (b) all-closed → skip holistic, (c) mid-state non-firing.
- 1.7.4 — `rule_cascade_close_on_merge` returns `CascadeCloseLeaves(epic_task_id, leaf_task_ids)` when the epic has `lifecycle=merged` and `_leaves_pending_cascade_close(task)` returns a non-empty list. Idempotent — already-closed leaves are excluded by the helper. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_cascade_close.py` covers (a) cascade fires for audit-pending leaves, (b) all-already-closed → no-op, (c) leaf-list correctness.
- 1.7.5 — `rule_archive_plan_on_merge` returns `ArchivePlan(epic_task_id, plan_id)` when the epic has `lifecycle=merged, status=closed`, has `task_artifacts.plan_file_path`, and the plan is not already archived in the `plans` table (§2.15). file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_archive_plan.py` covers (a) fires on terminal close with active plan, (b) no-op when plan already archived, (c) no-op when no plan_file_path artifact.
- 1.7.6 — RULES tuple ordering: `rule_qa` precedes `rule_all_leaves_holistic`; `rule_cascade_close_on_merge` and `rule_archive_plan_on_merge` are appended after `rule_merging`. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_ordering.py`.

### 1.8 Lifecycle transitions in review tools [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_transitions.py` and the matching MCP wrappers

**Core contract**: when `mark_task_review_approved` triggers a lifecycle advance, it also **resets `status` to `open`** so the next stage's rule can dispatch (rules gate on `status in ("open", "needs_review")`). The approval event is preserved in `task_lifecycle_events` (§1.1c) — audit trail is not lost.

Extend existing transitions and add one new one:

- **`mark_task_review_approved`**:
  - `lifecycle=plan_review` → advance to `test_arch`; status resets to `open`.
  - `lifecycle=test_arch` → advance to `expanding`; status resets to `open`.
  - `lifecycle=expanding` → advance to `in_development`; status resets to `open`. (Called by expansion-qa after successful validation — §2.9 wires this.)
  - `lifecycle=holistic_review` (epic) → **advance to `pr`** (R3.F4 fix); status resets to `open`. If `pr` stage is skipped, further advance to `merging` (or `merged` if both are skipped).
  - `lifecycle=merging` → advance to `merged` (terminal); status = `closed`.
  - `lifecycle=in_development` (leaf — qa-reviewer caller) → advance the leaf to `lifecycle=holistic_review` and set `status=review_approved`. Leaf does **not** close — it parks at `holistic_review + review_approved` until the parent epic merges, at which point `rule_cascade_close_on_merge` (§1.7) cascades the leaf to `status=closed`. The atomic leaf transition is what trips `rule_all_leaves_holistic` (§1.7) once every sibling reaches a terminal-or-holistic state. The transition writes a `task_lifecycle_events` row with `reason="mark_task_review_approved"` and `by_actor=<current_session_agent_name>` (typically `qa-reviewer` or `dispatcher` for yolo fallbacks).
  - Every advance writes a `task_lifecycle_events` row with `reason="mark_task_review_approved"` and `by_actor=<current_session_agent_name>`.

- **`mark_task_review_rejected(task_id, rejection_notes=None, round_number=None, cited_subtasks=None)`**: extended signature — `cited_subtasks` is a list of leaf refs (R4.F5). Behavior by lifecycle:
  - `lifecycle=plan_review` → stays at `plan_review`; adds `planning-current-verdict:rejected` label; increments `planning-round:N`; appends findings to description (existing behavior per R2.F1). `cited_subtasks` ignored.
  - `lifecycle=holistic_review` (R4.F5 fix; epic only) → `cited_subtasks` is **REQUIRED** (one or more leaf refs needing rework); rejection without it raises a validation error. The tool atomically (single transaction): (a) appends findings to the epic description, (b) reopens each cited subtask (`status: review_approved | closed → open`; `lifecycle: holistic_review → in_development` for leaves rewound from the audit-pending park), and (c) rewinds the epic lifecycle `holistic_review → in_development` with `status=open`. The atomic reopen prevents `rule_all_leaves_holistic` (§1.7, renamed) from immediately bouncing the epic back into `holistic_review` on the next tick (because at least one subtask is now `open` at `in_development`, the predicate is false until the dev/qa loop drives the cited leaves back to terminal-or-holistic). The escalate-rescope third path (below) is the only no-cited rejection mechanism — bare rejection without `cited_subtasks` is invalid by design.
  - `lifecycle=expanding` (R4.F1 extension) → stays at `expanding`; findings appended; **clears `task_artifacts.expansion_run_id`** (so `rule_start_expansion` can re-fire on the next tick); **calls `increment_expansion_attempts(task_id)`** so the retry cap is enforced. §2.9 (expansion-qa) is the caller.
  - `lifecycle=merging` (R6.F4 extension) → stays at `merging`; status resets to `open`; findings appended (yolo retry detail or non-yolo failure note); **does NOT itself manipulate the `merge-attempts:N` label** — that label is managed by the merge agent (§2.10) immediately before the rejection call. The tool atomically (single transaction) appends findings, resets status, and writes the rejection event to `task_lifecycle_events`. `rule_merging` re-dispatches the merge agent on the next tick. §2.10 (merge agent yolo retry path) is the caller. After `merge-attempts:N >= cap`, the merge agent switches to the force-advance fallback (`mark_task_review_approved` with audit marker), which §2.10 documents in detail.
  - Leaf `lifecycle=in_development` (qa-reviewer caller) → no lifecycle change; `status: needs_review → open`; rejection_notes appended to description; **increments `qa-attempts:N` label** (matching the cap-enforcement contract in `rule_qa`, §1.7 — `MAX_QA_ROUNDS=5`). The leaf re-enters the dev/qa loop on the next tick. `cited_subtasks` ignored. After the cap, behavior is rule-driven (escalate non-yolo / force-advance yolo per `rule_qa` cap exhaustion).

- **Holistic-review escalate-rescope (third path)**: when the holistic-reviewer determines the plan premise is wrong rather than the implementation, it calls `escalate_task(task_id=epic, reason="needs_human:rescope_required:<details>")` (or `reason="needs_human:requirements_unclear:<details>"`). The tool flips the epic to `status=escalated`, leaves `lifecycle=holistic_review` unchanged, and writes a `task_lifecycle_events` row. The user resumes via the existing extended `de_escalate_task(epic, next_status=..., lifecycle=..., reason=...)` after revising the plan or accepting the rework scope. Validation: `escalate_task` rejects reasons that don't start with `needs_human:rescope_required:` or `needs_human:requirements_unclear:` when the caller is the holistic-reviewer agent (enforced via `by_actor` check); other agents may use other `needs_human:` prefixes per existing escalation conventions.

- **`mark_task_needs_review`**: no lifecycle change. Planner MUST clear the `planning-current-verdict:rejected` label when submitting for review (R2.F1, enforced in §2.7 planner step definition).

- **New tool `advance_lifecycle(task_id, to, reason, by_actor)`**: MCP-exposed explicit transition. `reason` is **mandatory** (TEXT NOT NULL in `task_lifecycle_events`); `by_actor` defaults to the calling session's agent name. Writes the row and updates `tasks.lifecycle`. Also resets `status` to `open` unless the new lifecycle is `merged` (terminal → `closed`).

- **Extended tool `de_escalate_task(task_id, next_status, lifecycle=None, reason=None)`**: now accepts an optional `lifecycle` parameter for single-call recovery. Matters for pr-escalation and holistic-rescope: after a human opens a PR, they run `de_escalate_task(task_id, next_status="review_approved", lifecycle=Lifecycle.merging, reason="human opened PR #N")`; after a rescope-escalation, they revise the plan and run `de_escalate_task(epic, next_status="open", lifecycle=Lifecycle.in_development, reason="rescoped per revised plan")`. The tool:
  1. Clears the escalated state, sets `status = next_status`.
  2. If `lifecycle` is provided, also calls `advance_lifecycle(task_id, to=lifecycle, reason=reason or "de-escalation", by_actor="human")`.
  3. Writes the combined change in a single transaction.

Session-context enforcement stays as today (mark_* autonomous-only; close_task interactive unless escaping with labels). §2.14a extends this to scope interactive-only hooks (e.g., `require-task-close`) so they do not block autonomous build agents calling `mark_task_*`; §2.14a also requires `mark_task_review_*` to run the same validation gates as `close_task` (commit-attached, validation_criteria, errors_resolved, memory_review_completed) — there is no `skip_validation` for autonomous build agents.

**Acceptance:**

- 1.8.1 — Lifecycle transitions in review tools is implemented according to this section. file: `src/gobby/storage/tasks/_transitions.py`.
- 1.8.2 — `mark_task_review_approved` on a leaf at `lifecycle=in_development` advances to `lifecycle=holistic_review, status=review_approved` atomically (DB transaction + `task_lifecycle_events` row in one commit). Leaf does NOT close. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_qa_review_transitions.py::test_leaf_approve_advances_to_holistic`.
- 1.8.3 — `mark_task_review_rejected` on a leaf at `lifecycle=in_development` increments `qa-attempts:N` label, sets `status=open`, appends rejection_notes, leaves `lifecycle=in_development`. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_qa_review_transitions.py::test_leaf_reject_increments_qa_attempts`.
- 1.8.4 — `mark_task_review_rejected(epic, lifecycle=holistic_review)` without `cited_subtasks` raises a validation error (R4.F5 invariant preserved). The escalate-rescope third path via `escalate_task(epic, reason="needs_human:rescope_required:..."|"needs_human:requirements_unclear:...")` is the only no-cited rejection mechanism. test: `tests/storage/test_holistic_rejection.py::test_bare_rejection_invalid_and_escalate_rescope_path`.
- 1.8.5 — `mark_task_review_rejected(epic, lifecycle=holistic_review, cited_subtasks=[leaf_ids])` atomically rewinds: cited leaves go from `(holistic_review, review_approved)` (or `(holistic_review, closed)` if cascade ran) to `(in_development, open)`; epic goes to `(in_development, open)`. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_holistic_rejection.py::test_cited_rewinds_leaves_and_epic`.
- 1.8.6 — `mark_task_review_*` transitions run the same validation gates as `close_task` (commit-attached, validation_criteria pass, errors_resolved, memory_review_completed); `skip_validation` is not honored. behavior: integration test in `tests/storage/test_mark_task_review_validation.py` covers a missing-commit rejection and a passing-gate approval. (Cross-reference §2.14a.5.)
- 1.8.7 — Extended `de_escalate_task(task_id, next_status, lifecycle, reason)` performs the combined status + lifecycle change in a single transaction; `task_lifecycle_events` records both rows (de-escalate and lifecycle advance) atomically. test: `tests/storage/test_de_escalate.py::test_combined_status_lifecycle`.

### 1.9 Dispatcher scanner [category: code] (depends: 1.4, 1.6, 1.7, 1.8)
`kind: deliverable`

Target: `src/gobby/dispatch/dispatcher.py` (new file)

```python
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
import json

from gobby.agents.spawn_executor import SpawnRequest, execute_spawn
from gobby.dispatch import mutex, rules
from gobby.dispatch.actions import (
    AdvanceLifecycle, AppendAuditMarker, CreateWorktree, EscalateTask,
    Skip, SpawnAgent, StartExpansionRun,
)
from gobby.dispatch.prompts import PROMPT_BUILDERS
from gobby.storage.database import LocalDatabase
from gobby.storage.tasks._artifacts import set_artifact
from gobby.storage.tasks._crud import list_automation_candidates
from gobby.storage.tasks._transitions import advance_lifecycle, append_description_section
from gobby.tasks.expansion_service import ExpansionService
from gobby.worktrees.git import WorktreeGitManager

@dataclass
class TickReport:
    started_at: str = ""
    finished_at: str = ""
    swept: int = 0
    dispatched: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

MAX_ACTIVE_AGENTS_DEFAULT = 10   # configurable via daemon config
DISPATCHER_LOG = Path.home() / ".gobby" / "logs" / "dispatcher.jsonl"

async def run_tick(db: LocalDatabase, holder: str, max_active: int) -> TickReport:
    report = TickReport(started_at=datetime.now(UTC).isoformat())
    report.swept = mutex.sweep_on_startup(db)          # no-op after first tick
    active = _count_active_autonomous_agents(db)
    for task in list_automation_candidates(db):
        if active >= max_active:
            report.skipped.append((task.id, "agent slot cap reached"))
            continue
        action = rules.evaluate(task)
        if isinstance(action, Skip):
            report.skipped.append((task.id, action.reason))
            continue
        # Acquire mutex for the duration of evaluate-and-act:
        primary_action = action if not isinstance(action, list) else action[-1]
        kind = _action_kind(primary_action)
        agent_run_id = f"run-{uuid4().hex[:12]}" if isinstance(primary_action, SpawnAgent) else None
        with mutex.acquire(db, task.id, holder, kind, agent_run_id=agent_run_id) as ok:
            if not ok:
                report.skipped.append((task.id, f"mutex contended ({kind})"))
                continue
            # Re-evaluate under lock to close TOCTOU window:
            fresh = _reload_task(db, task.id)
            action = rules.evaluate(fresh)
            if isinstance(action, Skip):
                report.skipped.append((task.id, action.reason))
                continue
            try:
                actions = action if isinstance(action, list) else [action]
                for a in actions:
                    await _dispatch(db, fresh, a, agent_run_id, report)
                if any(isinstance(a, SpawnAgent) for a in actions):
                    mutex.detach_from_context(task.id)
                    active += 1
            except Exception as exc:
                report.errors.append((task.id, str(exc)))
    report.finished_at = datetime.now(UTC).isoformat()
    _persist_tick_report(report)
    return report

async def _dispatch(db, task, action, agent_run_id, report):
    match action:
        case SpawnAgent() as s:
            prompt, initial_vars = PROMPT_BUILDERS[s.prompt_builder](task)
            req = SpawnRequest(
                prompt=prompt,
                cwd=_resolve_cwd(task),
                provider=_resolve_provider(s),
                session_id=str(uuid4()),
                run_id=agent_run_id,
                agent_run_id=agent_run_id,
                parent_session_id=_dispatcher_parent_session(),
                project_id=task.project_id,
                session_manager=_session_manager(),
                machine_id=_machine_id(),
                task_id=task.id,
                title=f"{s.agent} for {task.id}",
                model=s.model_override,
                requested_reasoning_effort=s.reasoning_effort,
                initial_variables={**(s.initial_variables or {}), **initial_vars,
                                   "additional_skills": s.additional_skills or []},
                workflow=_workflow_for_agent(s.agent),
                agent_name=s.agent,
                timeout_seconds=_timeout_for_agent(s.agent),
            )
            result = await execute_spawn(req)
            if not result.success:
                raise RuntimeError(f"execute_spawn failed: {result.error}")
            report.dispatched.append(task.id)
        case StartExpansionRun(task_id, tdd):
            # R6.F5 / R7.F2: route through the existing `gobby-tasks-ops:start_expansion_run`
            # MCP tool's underlying Python handler, which already builds the
            # required `LocalExpansionRunManager.create(parent_task_id=...,
            # project_id=..., triggering_session_id=..., input_source=...)`
            # call with the right metadata, kicks off the background compile,
            # and returns the run record. We import the impl directly (the MCP
            # tool is a stateless wrapper). On compile-side exceptions, the
            # handler surfaces a failed run via its standard error path; we
            # still persist the id so `rule_validate_expansion` and
            # `expansion-qa` (§2.9) can observe failure and reject.
            from gobby.mcp_proxy.tools.tasks_ops import start_expansion_run_impl
            from gobby.storage.tasks._artifacts import set_artifact
            run = await start_expansion_run_impl(
                task_manager=_task_manager(db),
                llm_service=_llm_service(),
                config=_daemon_config(),
                parent_task_id=task_id,
                triggering_session_id=_dispatcher_session_id(),
                input_source=("plan_file" if _has_plan_file(db, task_id) else "epic"),
                tdd=tdd,
            )
            set_artifact(db, task_id, "expansion_run_id", run.id)
            report.dispatched.append(task_id)
        case CreateWorktree(epic_task_id, base_branch) | CreateClone(epic_task_id, base_branch):
            # R6.F1 / R6.F3 / R7.F1: both isolation paths go through the real
            # high-level API — `WorktreeIsolationHandler.prepare_environment(SpawnConfig)`
            # and `CloneIsolationHandler.prepare_environment(SpawnConfig)` — both
            # in `src/gobby/agents/isolation.py`. Each returns an
            # `IsolationContext(cwd, branch_name, worktree_id|clone_id, isolation_type, extra)`
            # where `cwd` is the worktree-or-clone path. The handlers internally
            # compose storage `.create(...)`, git-manager `.create_worktree(...)`/
            # `.create_clone(clone_path, branch_name, base_branch, ...)`, and the
            # post-creation bootstrap (hook copy, project.json sync, MCP patching).
            # We persist `(cwd, ctx.worktree_id|clone_id)` into task_artifacts as
            # a single atomic pair-write so the §1.1b CHECK constraint never sees
            # a half-populated state.
            from gobby.agents.isolation import (
                CloneIsolationHandler, SpawnConfig, WorktreeIsolationHandler,
            )
            from gobby.storage.tasks._artifacts import set_artifacts_atomic
            handler_cls = (WorktreeIsolationHandler
                           if isinstance(action, CreateWorktree)
                           else CloneIsolationHandler)
            handler = _build_isolation_handler(db, handler_cls, epic_task_id)
            cfg = _build_spawn_config(db, epic_task_id, base_branch=base_branch)
            ctx = await handler.prepare_environment(cfg)
            if ctx.isolation_type == "worktree":
                set_artifacts_atomic(db, epic_task_id,
                                     worktree_path=ctx.cwd, worktree_id=ctx.worktree_id)
            else:
                set_artifacts_atomic(db, epic_task_id,
                                     clone_path=ctx.cwd, clone_id=ctx.clone_id)
            report.dispatched.append(epic_task_id)
        case AdvanceLifecycle(task_id, to, reason, by_actor):
            advance_lifecycle(db, task_id, to, reason=reason, by_actor=by_actor)
            report.dispatched.append(task_id)
        case EscalateTask(task_id, reason):
            from gobby.storage.tasks._transitions import escalate_task
            escalate_task(db, task_id, reason=reason)
            report.dispatched.append(task_id)
        case AppendAuditMarker(task_id, heading, body):
            append_description_section(db, task_id, heading=heading, body=body)
            # Not reported as "dispatched" — purely an audit side-effect.

def _persist_tick_report(report: TickReport) -> None:
    """Append a structured line to ~/.gobby/logs/dispatcher.jsonl.
    Caller must handle rotation (planned as a future daemon-level concern;
    first cut just appends indefinitely and lets logrotate handle size)."""
    DISPATCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DISPATCHER_LOG.open("a") as fh:
        fh.write(json.dumps(asdict(report)) + "\n")
```

`_action_kind` maps each `Action` subclass to an `ActionKind` for the mutex (`CreateClone` and `CreateWorktree` both map to `"worktree"` kind — same TTL/contention class). `_build_isolation_handler(db, handler_cls, epic_task_id)` constructs the appropriate handler with its real dependencies — `WorktreeIsolationHandler(worktree_storage, git_manager)` or `CloneIsolationHandler(clone_storage, clone_manager, git_manager=...)`; both pull from existing daemon services (`db.local_worktree_manager`, `db.local_clone_manager`, `WorktreeGitManager(repo_path)`, `CloneGitManager(repo_path)`). `_build_spawn_config(db, epic_task_id, *, base_branch)` fills a `SpawnConfig` (`src/gobby/agents/isolation.py`) with `project_id`, `project_path`, `provider`, `task_id`, `base_branch`, and any other fields the handlers' branch-name generation requires. `_resolve_cwd(task, agent_name)` returns the appropriate working directory for the agent. **Dev/QA agents** running on a leaf get the parent epic's isolation artifact: `task_artifacts.clone_path` when `task.isolation == clone`, `task_artifacts.worktree_path` when `worktree`, repo root when `none`; the resolution walks up to `_parent_epic(task)` for leaves. **The merge agent** is the exception (R6.F3): it runs in the source repo regardless of isolation, because the `gobby-worktrees:merge_worktree` and `gobby-clones:merge_clone` tools manage paths internally and require the source-repo cwd. So when `agent_name == "merge"`, `_resolve_cwd` returns the repo root unconditionally; the dispatcher passes `worktree_id` / `clone_id` and `target_branch` as `initial_variables` instead. `_clones_dir()` reads `BuildConfig.clones_dir` (§3.1, default `~/.gobby/clones/`). `_count_active_autonomous_agents` queries `running_agents` for sessions flagged autonomous (the spawn path tags them). `_reload_task` re-queries the row under the mutex to avoid TOCTOU. `append_description_section` in `_transitions.py` is a new helper that appends a `## {heading}\n{body}\n` block to the task description (idempotent by (task_id, heading, body) signature — duplicate markers within the same tick are deduped).

**Acceptance:**

- 1.9.1 — Dispatcher scanner is implemented according to this section. file: `src/gobby/dispatch/dispatcher.py`.

### 1.10 Cron handler registration [category: code] (depends: 1.9)
`kind: deliverable`

Target: `src/gobby/dispatch/cron_registration.py` (new) + `src/gobby/runner_init.py` (add call)

```python
# src/gobby/dispatch/cron_registration.py
from gobby.dispatch.dispatcher import MAX_ACTIVE_AGENTS_DEFAULT, run_tick
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron_models import CronJob

def register_state_dispatcher(executor: CronExecutor, db, config) -> None:
    max_active = config.get("dispatch", {}).get("max_active_agents",
                                                 MAX_ACTIVE_AGENTS_DEFAULT)
    async def handler(job: CronJob) -> str:
        holder = f"state-dispatcher:{job.id}"
        report = await run_tick(db, holder, max_active)
        return (f"dispatched={len(report.dispatched)} "
                f"skipped={len(report.skipped)} "
                f"swept={report.swept} errors={len(report.errors)}")
    executor.register_handler("state-dispatcher", handler)

def ensure_state_dispatcher_cron_row(db, project_id: str) -> None:
    """Idempotent: insert a cron_jobs row if one doesn't exist for this project."""
    ...
```

In `runner_init.py`, after `CronExecutor` instantiation and before `CronScheduler.start()`:

```python
from gobby.dispatch.cron_registration import (
    ensure_state_dispatcher_cron_row, register_state_dispatcher,
)
register_state_dispatcher(executor, db, config)
ensure_state_dispatcher_cron_row(db, project_id)
```

The cron_jobs row:

```python
{
    "id": "state-dispatcher-main",
    "project_id": project_id,
    "name": "state-dispatcher",
    "schedule_type": "interval",
    "schedule_expression": "60s",
    "action_type": "handler",
    "action_config": {"handler": "state-dispatcher"},
    "enabled": True,
}
```

**Acceptance:**

- 1.10.1 — Cron handler registration is implemented according to this section. file: `src/gobby/dispatch/cron_registration.py`.

## P2 Phase 2: New Agents + Stage Integration
`kind: framing`

**Goal**: Fill in the agents and the skill the new rules reference, wire existing agents to close leaves themselves, and make expansion profile-aware.

### 2.8 Expansion: Agent Selection + profile-appropriate subtasks [category: code]
`kind: deliverable`

> **Status: partial** — Per-acceptance-item state: `2.8.1` (stage-driven tree shape) and `2.8.2` (`assigned_agent` population on automated leaves) shipped via commit `36a48d3e` at `src/gobby/tasks/expansion_service.py`; the section's originally named `src/gobby/tasks/expansion.py` path was not used (path drift — see corrected acceptance items below). `2.8.3` (planning-leaf rejection + test-category-leaf infrastructure-only validation) is **pending** — those checks are missing from `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` and land via §2.9 wiring.

Target: `src/gobby/tasks/expansion_service.py` and `src/gobby/tasks/expansion.py`

Expansion has two responsibilities: shaping the subtask tree based on which stages are enabled, and assigning each dev leaf to an agent from the registry.

**Stage-driven tree shape** (no STAGE_BY_PROFILE; reads `_skipped_stages(epic)`):

- If `dev` is the only non-skipped stage (e.g., CLI profile "quick"): no expansion happens — `gobby build` set the epic straight to `lifecycle=in_development` with the task as its own sole leaf. Dev handles the whole task.
- If `qa` is enabled: generated subtasks; QA closes each leaf on approval.
- If `worktree` isolation is `worktree`: worktree created once for the epic (§1.7 `rule_create_worktree`); each subtask's dev/qa runs within it.
- If `worktree` isolation is `none`: dev and QA work on the current branch.

**Agent Selection** (R3.U4 — replaces stack/skill annotation; R4.F3 — covers all automated leaf categories):

- Every generated leaf in an automated category (`AUTOMATED_LEAF_CATEGORIES = {code, config, docs, test}`, §1.7) gets an `assigned_agent: str` column value. The expander calls `list_agent_definitions` on gobby-workflows and picks the best-fit agent from the registry based on the leaf's content (description, category, labels), guided by the §2.8a heuristics.
- `category: planning` leaves are **NOT permitted** by expansion. Planning is for epics; a `planning` leaf is an expansion bug. `expansion-qa` rejects any expansion run that emits one (§2.9, R4.F3).
- Every automated leaf *optionally* gets `additional_skills: list[str]` when the leaf needs skills beyond the agent's baseline. Default empty.
- **Mixed-stack work**: expander splits into per-agent leaves when the plan naturally permits it (e.g., "wire API + render results" splits into a backend-developer leaf for the endpoint and a frontend-developer leaf for the consumer). When a leaf can't be cleanly split, pick the primary agent and note the cross-concern surface in the description.
- **No escalation on ambiguity**: when no agent scores above the confidence threshold, default to `backend-developer` and append an `## Agent Selection` marker to the leaf description explaining the default (R3.U10 tracked via the `expansion-agent-selection` skill, §2.8a). Audit these over time to decide whether to build a new agent or tune the expander prompt.

**Prompt change**: the expander prompt gains an "Agent Selection" step after leaf generation. That step:
  1. Load the `expansion-agent-selection` skill (§2.8a) — gives the expander the label vocabulary and agent-registry decision heuristics for every automated category.
  2. For each generated leaf in an automated category, call `list_agent_definitions` (cached per run).
  3. Pick the best-fit agent; emit `assigned_agent` on the leaf.
  4. Decide if `additional_skills` are needed on top of the agent's baseline; emit if yes.

**After-run check**: `expansion-qa` validates that generated tasks match the plan's `### N.N` sections, that every automated-category leaf has `assigned_agent` populated (non-null), that no leaf has `category: planning`, and that any `[category: test]` leaf description clearly identifies test infrastructure rather than authored test cases (R4.F4 boundary). Any of these is a validation failure that re-opens the expansion run via `mark_task_review_rejected` (§2.9 wires the transition; §1.8 clears `expansion_run_id` so `rule_start_expansion` can re-fire).

**Acceptance:**

- 2.8.1 — Expansion emits stage-driven task trees from `_skipped_stages(epic)` state instead of stored profiles. file: `src/gobby/tasks/expansion_service.py`.
- 2.8.2 — Every automated leaf in code, config, docs, or test categories receives an `assigned_agent` value (and optional `additional_skills`) selected via the `expansion-agent-selection` heuristic against `list_agent_definitions`; ambiguous leaves default to `backend-developer` and emit an `## Agent Selection` description marker. file: `src/gobby/tasks/expansion_service.py`.
- 2.8.3 — Expansion-QA rejects any expansion run that emits a `category: planning` leaf and rejects any `[category: test]` leaf whose description does not clearly identify test infrastructure (fixtures, helpers, conftest, harness modules). file: `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`. behavior: rejection cites the specific offending leaf in `rejection_notes`.

### 2.8b Expose `start_expansion_run_impl` for in-process dispatcher use [category: code] (depends: 1.1b)
`kind: deliverable`

> **Status: partial** — nested MCP handler `start_expansion_run` shipped via commit `4ee54dc5` at `src/gobby/mcp_proxy/tools/tasks/_expansion.py`; the importable `start_expansion_run_impl` symbol required by §1.9 is not exported.

Target: `src/gobby/mcp_proxy/tools/tasks_ops.py` (or wherever the MCP tool's handler lives)

R6.F5 + R7.F2 fix. The existing `gobby-tasks-ops:start_expansion_run` MCP tool already wraps the canonical "create expansion-run row + kick compile + return run record" flow against the real `ExpansionService(*, task_manager, llm_service, config=None, run_manager=None)` constructor and `LocalExpansionRunManager.create(*, parent_task_id, project_id, triggering_session_id, input_source, plan_file=None, provider=None, model=None, options=None, run_id=None) -> ExpansionRun`. The dispatcher (§1.9 `StartExpansionRun` case) calls the same handler directly, in-process, under its mutex hold. No new `ExpansionService.start_run` wrapper needed — the impl already exists.

This task: ensure the underlying handler is exported as `start_expansion_run_impl` (or equivalent name) so the dispatcher can `from gobby.mcp_proxy.tools.tasks_ops import start_expansion_run_impl` rather than going through the MCP transport layer. Verify the impl returns the `ExpansionRun` (or at minimum `run.id`); if today it only returns a serialized dict, refactor to return the underlying record alongside its dict form, or have it return `run.id` directly. Add a unit test asserting the impl returns the run id and that compile failures still produce a row whose `id` the caller can persist.

The dispatcher captures `run.id` and writes it into `task_artifacts.expansion_run_id` (§1.9). Compile failures are recoverable: `rule_validate_expansion` waits for the run to reach a terminal state (completed OR failed); on failed, expansion-qa picks it up and rejects via `mark_task_review_rejected(lifecycle=expanding)`, which clears `expansion_run_id` and increments `expansion_attempts` (§1.8 R4.F1 extension).

**Acceptance:**

- 2.8b.1 — Expose start_expansion_run_impl for in-process dispatcher use is implemented according to this section. file: `src/gobby/mcp_proxy/tools/tasks_ops.py`.

### 2.9 Expansion-QA transition contract [category: config] (depends: 1.8, 1.1b)
`kind: deliverable`

> **Status: partial** — plan-coverage and review wiring shipped via commit `4ee54dc5` at `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`; the `assigned_agent`, `category: planning`, and test-infrastructure-only `[category: test]` checks named in the acceptance items are pending.

Target: `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`

R3.F3 fix + R4.F1/F3/F4 extensions. Today's `expansion-qa` validates the expansion run but has no owned transition contract on the parent epic — the plan relied on it without wiring the call. Update the YAML to:

1. **On validation success** (all `### N.N` sections present as subtasks; every automated-category leaf has `assigned_agent`; no `category: planning` leaves; `[category: test]` leaves are infrastructure not authored cases):
   - `mark_task_review_approved(parent_task_id)` — which, per §1.8, advances `lifecycle: expanding → in_development` and resets status to `open`. The `task_artifacts.expansion_run_id` and `expansion_attempts` fields are NOT cleared on approval (kept as audit trail of which run produced the approved tree).
2. **On validation failure** — call `mark_task_review_rejected(parent_task_id, rejection_notes=<findings>)`. Per §1.8 (R4.F1 extension), this leaves the epic at `lifecycle=expanding, status=open`, **clears `task_artifacts.expansion_run_id`** (so `rule_start_expansion` can re-fire on the next tick), and **increments `task_artifacts.expansion_attempts`**. After `MAX_EXPANSION_ATTEMPTS` (default 3, defined in §1.7), `rule_start_expansion` either escalates (non-yolo) or force-advances with an audit marker (yolo).

   Validation failures that trigger rejection:
   - **Generic**: missing `### N.N` sections, malformed subtask shapes, mismatched parent linkage.
   - **R4.F3** Missing `assigned_agent` on any automated-category leaf (`code | config | docs | test`). Finding cites the specific leaf: "agent selection pass did not populate assigned_agent for <leaf ref>."
   - **R4.F3** Any leaf with `category: planning`. Finding cites the leaf: "planning category not permitted on leaves; promote to a separate epic or change category."
   - **R4.F4** Any `[category: test]` leaf whose description does not clearly identify test infrastructure (fixtures, helpers, conftest, harness modules). Authored test cases belong inside `[category: code]` leaves' TDD sandwiches. Finding cites the leaf: "test leaf appears to author test cases rather than infrastructure; relocate to the affected code leaf's TDD step."

Unblock `mark_task_review_approved` and `mark_task_review_rejected` in the agent's allowlist for the validation step. Existing task-transitions skill gates (autonomous-only for `mark_*`) still apply.

**Acceptance:**

- 2.9.1 — Expansion-QA transition contract is implemented according to this section. file: `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`.

### 2.10 Merge agent — lifecycle integration [category: config] (depends: 1.8, 1.1b)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/merge.yaml`

R4.F7 fix. The shipped `merge.yaml` is a pure merge-tool runner: it runs `git merge` and `kill_agent`, but never calls `mark_task_review_*` or writes back to task lifecycle. Without an explicit transition-writing step, `rule_merging` dispatches the agent and the epic stays at `lifecycle=merging` forever. This task makes `merge.yaml` a faithful citizen of the dispatcher's lifecycle state machine.

Keep the existing tool-driven contract that the shipped `merge.yaml` already implements (R6.F3 / R7.F3 fix). Do NOT switch to raw `git` shell calls — `merge.yaml` forbids Bash on purpose, and the existing tools handle everything: `gobby-worktrees:merge_worktree` (with `gobby-merge:merge_*` AI resolution for worktree conflicts), `gobby-clones:sync_clone` + `gobby-clones:merge_clone`. The fix is purely about (a) wiring the dispatcher to spawn the merge agent with the right session variables, (b) extending the agent's finalize step to write task lifecycle, and (c) defining the post-merge cleanup contract using the real tool names.

**Dispatcher side** (recap, no new pseudo-APIs):
- `rule_merging` already emits `SpawnAgent(agent="merge", ...)`. The `merge_runner` prompt builder (§1.6 PROMPT_BUILDERS) reads `task_artifacts` via `gobby-tasks-ops:get_artifacts` (§1.1d) and passes `worktree_id` OR `clone_id` (per the §1.1b CHECK XOR) and `target_branch` as `initial_variables`. The shipped `merge.yaml` already branches on which session var is present.

**`merge.yaml` finalize-step extensions** (the only YAML changes):
- After the existing tool flow returns success, the agent calls `mark_task_review_approved(task_id=epic_task_id, approval_notes="merge completed")` (the tool's real param is `approval_notes`, not `reason`). §1.8 advances `lifecycle: merging → merged, status: closed`.
- **Worktree cleanup**: call `gobby-worktrees:mark_worktree_merged(worktree_id)` so the worktrees registry reflects the merged state, then `gobby-worktrees:delete_worktree(worktree_id)` (the real tool name; `remove_worktree` does NOT exist) for filesystem teardown. Then clear the artifact pair atomically via `gobby-tasks-ops:clear_isolation_pair(epic_task_id, "worktree")` (§1.1d). Worktree cleanup is unconditional on success today.
- **Clone cleanup**: if `BuildConfig.cleanup_clones_on_merge` is true (§3.1), call `gobby-clones:delete_clone(clone_id)`, then `gobby-tasks-ops:clear_isolation_pair(epic_task_id, "clone")` (§1.1d). Otherwise leave both the clone on disk and the artifact row in place.
- **Merge SHA capture is deferred**: neither `merge_worktree` nor `merge_clone` currently returns `merge_commit_sha` in its response (verified via current code). Capturing it requires extending those tools, which is real implementation work; deferred to **#12728** alongside PR-creation. The `task_artifacts.merge_commit_sha` column is reserved for that future write but is left NULL on every merge in this epic. No raw `git` calls anywhere — the agent's tool surface forbids Bash.

**Failure paths** (rejection contract — `lifecycle=merging` rejection case in §1.8 R6.F4):

1. **Clean merge** — described above.
2. **Conflict — non-yolo** (worktree only; clone path has no AI resolution per shipped flow): the existing AI flow (`gobby-merge:merge_start` → `merge_status` → `merge_resolve` → `merge_apply`) attempts resolution. If resolution succeeds → success path. If resolution fails (`merge_abort` is called by the agent), the agent calls `escalate_task(task_id=epic_task_id, reason="needs_human: merge conflict on <target_branch>; resolve manually and call de_escalate_task(task_id, target_status='review_approved', lifecycle=Lifecycle.merged, reason='human resolved conflict')")`. For clone conflicts, the same escalate path is taken directly (no AI resolution attempt — clone has no `gobby-merge:*` integration).
3. **Conflict — yolo** (worktree): try AI resolution as in (2); on success, take the success path. On failure, increment a `merge-attempts:N` label on the epic via the existing `add_label` / `update_task` (labels) tools (default cap 3), call `mark_task_review_rejected(task_id=epic_task_id, rejection_notes="yolo conflict resolution attempt failed: <details>")` — see §1.8 R6.F4 for the `lifecycle=merging` rejection contract — then `kill_agent`. `rule_merging` re-dispatches on the next tick. (Clone-with-yolo: same retry pattern, but each attempt is a fresh `sync_clone` + `merge_clone`; no AI step.)
4. **Yolo conflict — retries exhausted** (`merge-attempts:N >= cap`): preserve the R3.U1 "yolo never escalates" invariant via the documented force-advance fallback. Call `gobby-tasks-ops:append_description_section(epic_task_id, heading="Yolo Fallbacks", body="<timestamp>: merge attempts exhausted (<N>); <worktree|clone> preserved at <path>; lifecycle force-advanced to merged without merge")` (§1.1d), then `mark_task_review_approved(task_id=epic_task_id, approval_notes="yolo: merge attempts exhausted, force-advanced; isolation artifact preserved")`. Lifecycle advances to `merged, status=closed`. The artifact pair is **NOT cleared** on this path — cleanup is skipped so a human can inspect (the artifact row stays populated with `worktree_path`/`worktree_id` or `clone_path`/`clone_id`). Documented exception under R3.U1.

**Allowlist additions** to merge.yaml's existing `allowed_mcp_tools`:

- `gobby-tasks:mark_task_review_approved`
- `gobby-tasks:mark_task_review_rejected`
- `gobby-tasks:escalate_task`
- `gobby-tasks:add_label` (for `merge-attempts:N` increments)
- `gobby-tasks-ops:get_artifacts` (initial read of `worktree_id`/`clone_id`/`target_branch` if not already in session vars)
- `gobby-tasks-ops:clear_isolation_pair` (§1.1d — clean up artifact pair atomically on success)
- `gobby-tasks-ops:append_description_section` (§1.1d — yolo-fallback audit marker)

The existing tool list (per shipped merge.yaml) already includes `gobby-worktrees:merge_worktree`, `gobby-clones:sync_clone`, `gobby-clones:merge_clone`, `gobby-merge:merge_*`, `gobby-agents:kill_agent` — those stay. Add `gobby-worktrees:mark_worktree_merged` and `gobby-worktrees:delete_worktree` for the cleanup step. Existing task-transitions skill gates (autonomous-only for `mark_*`) still apply.

Real PR-creation, merge-SHA capture, and AI-driven conflict resolution for clones remain in **#12728**. This task ships only the lifecycle handshake on top of the existing tool surface; no new git-driving code, no new merge mechanics.

**Acceptance:**

- 2.10.1 — Merge agent receives worktree_id or clone_id plus target_branch through dispatcher initial variables and reads artifacts when needed. file: `src/gobby/install/shared/workflows/agents/merge.yaml`.
- 2.10.2 — Clean merge success calls mark_task_review_approved and performs worktree or clone cleanup with the real MCP tool names. file: `src/gobby/install/shared/workflows/agents/merge.yaml`.
- 2.10.3 — Non-yolo conflicts escalate with needs_human instructions while preserving lifecycle state for recovery. behavior: `non-yolo merge conflict path in §2.10`.
- 2.10.4 — Yolo conflicts retry through mark_task_review_rejected and force-advance only after the documented attempt cap. behavior: `yolo merge conflict retry path in §2.10`.

### 2.11 qa-reviewer agent (read-only) [category: config] (depends: 1.7, 1.8)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml` (rewritten from scratch at this canonical path).

The currently-shipped `qa-reviewer.yaml` is read-write — the agent reviews AND fixes. The currently-shipped `qa-dev.yaml` is a sibling variant that fixes-and-approves end-to-end. Both pre-date the contract-driven build pipeline and conflate review with implementation. Both are deprecated under §2.14b: their files move to `src/gobby/install/shared/workflows/agents/deprecated/` (not hard-deleted), and bundled-sync removes their DB rows on next startup. The new `qa-reviewer.yaml` is written from scratch at the original path with a strictly read-only persona — Claude reviewing Codex's output. Claude is the rare resource (rate-limited, expensive); Codex is cheap, iterative, and absorbs rejections.

**Persona** (the prompt-step content, verbatim from the user-aligned design):

> Review it like you're an angry senior developer and this Codex agent is getting on your last nerve. Poke legitimate holes in their rationale if you can. Be ruthless, but not pedantic or passive aggressive. You just have very high standards and expect excellence from your team.

The persona is delivered in the agent's primary review-step prompt. It is not platitude-filtered — the prompt is meant to elicit hard pushback so the dev/qa loop converges on real fixes, not minimum-viable-passes.

**Tool surface (read-only)**:

- **Allowed**: `Read`; `gcode` via Bash for symbol navigation; `git diff/show/log/blame` via Bash; all `gobby-tasks` query tools (`get_task`, `list_tasks`, `search_tasks`, `get_dependency_tree`, `get_task_diff`, etc.); `gobby-tasks-ops:get_artifacts`, `gobby-tasks-ops:get_task_diff`; the three terminal transitions `mark_task_review_approved`, `mark_task_review_rejected`, `escalate_task`.
- **Blocked**: `Edit`, `Write`, `NotebookEdit`; mutating Bash (no `git commit`, `git add`, `git restore`, `git checkout`, no file writes via redirection / `tee` / `>`); `close_task`, `reopen_task`, `de_escalate_task`; all `set_artifact*` write tools. The blocklist is enforced via `denied_tools` plus a `denied_bash_substrings` allowlist on the agent's Bash configuration (matches the existing pattern used by holistic-reviewer for read-only Bash).

**Transition contract** (mirrors §1.7 `rule_qa` and §1.8 leaf-transition rules):

- Approve → `mark_task_review_approved(task_id, approval_notes=...)`. §1.8 transition advances the leaf to `lifecycle=holistic_review, status=review_approved`. Leaf parks until `rule_cascade_close_on_merge` fires (§1.7).
- Reject → `mark_task_review_rejected(task_id, rejection_notes=..., round_number=N)`. §1.8 transition flips the leaf to `status=open`, leaves `lifecycle=in_development`, increments `qa-attempts:N` label.
- Cap at `MAX_QA_ROUNDS=5` enforced by `rule_qa` (§1.7), not by the agent. The agent does not read or write `qa-attempts:N` directly — the storage transition manages it.

**Acceptance:**

- 2.11.1 — `qa-reviewer.yaml` rewritten from scratch at `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml` with the read-only persona; old shipped version moved to `deprecated/` (not deleted) per §2.14b. file: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`.
- 2.11.2 — `qa-dev.yaml` moved from `src/gobby/install/shared/workflows/agents/` to `src/gobby/install/shared/workflows/agents/deprecated/qa-dev.yaml` per §2.14b. behavior: bundled-sync removes the DB row on next startup; no rule references qa-dev.
- 2.11.3 — qa-reviewer's tool surface excludes `Edit`, `Write`, `close_task`, `reopen_task`, mutating Bash. test: `tests/agents/test_qa_reviewer_tool_surface.py` asserts the allowlist matches the read-only set; spawning the agent with attempted Edit usage fails the agent's denied_tools gate.
- 2.11.4 — Persona prompt text matches the verbatim user-provided string (above) in the review-step prompt of the agent yaml. file: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`.
- 2.11.5 — Transition contract: approve sets leaf to `(lifecycle=holistic_review, status=review_approved)`; reject sets leaf to `(lifecycle=in_development, status=open)` with `qa-attempts:N` incremented. test: `tests/storage/test_qa_review_transitions.py` covers both paths; cross-references §1.8.2 / §1.8.3.

### 2.12 holistic-reviewer three-outcome contract [category: config] (depends: 1.7, 1.8, 2.11)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml` (extend existing) and `src/gobby/install/shared/skills/holistic-review/SKILL.md` (extend existing).

The shipped agent has two outcomes — approve, reject-with-cited. The user's design adds a third for cases where the plan premise is wrong rather than the implementation. Doc audit lands in scope for 0.4.0 (holistic-reviewer audits docs alongside the code diff); a doc-only reviewer agent is **deferred to post-0.4.0** and is not in this epic.

**Three outcomes**:

- **Approve** → `mark_task_review_approved(epic, approval_notes=...)`. §1.8 advances epic `holistic_review → pr` (or further per stage-skip labels).
- **Reject with cited_subtasks** → `mark_task_review_rejected(epic, rejection_notes=..., cited_subtasks=[<leaf_refs>])`. §1.8 R4.F5 contract: cited leaves rewind from `(holistic_review, review_approved | closed)` to `(in_development, open)`; uncited leaves stay at `(holistic_review, review_approved)`; epic rewinds to `(in_development, open)`. The dev/qa loop re-runs only on cited leaves.
- **Escalate for rescope** → `escalate_task(epic, reason="needs_human:rescope_required:<details>")` OR `reason="needs_human:requirements_unclear:<details>"`. §1.8 third-path: epic flips to `status=escalated, lifecycle=holistic_review` (lifecycle unchanged); user resumes via `de_escalate_task(epic, next_status=..., lifecycle=...)` after revising the plan or accepting the rework scope. Used when implementation is fine but the spec is wrong.

**Doc audit in scope**: holistic-reviewer reads `docs/`, `CLAUDE.md`, README files affected by the diff, and any plan files referenced via `task_artifacts.plan_file_path`; missing or stale documentation is a rejection reason on its own (typically a cited-subtasks rejection naming the leaf that should have updated docs, OR an approval with a follow-up task created if docs are project-wide rather than leaf-scoped).

**Acceptance:**

- 2.12.1 — `holistic-reviewer.yaml` review-step prompt documents all three outcomes with the rejection reason taxonomy and the rescope-escalation prefixes (`needs_human:rescope_required:`, `needs_human:requirements_unclear:`). file: `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml`.
- 2.12.2 — `escalate_task` is in holistic-reviewer's allowed_mcp_tools list. file: `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml`.
- 2.12.3 — `holistic-review` skill loaded via `load_skill` covers doc-audit checks alongside code (read `docs/`, `CLAUDE.md`, plan files; flag stale or missing documentation). file: `src/gobby/install/shared/skills/holistic-review/SKILL.md`. test: `tests/agents/test_holistic_reviewer_outcomes.py` exercises approve / reject-cited / escalate-rescope paths against fixtures.
- 2.12.4 — `mark_task_review_rejected(epic, lifecycle=holistic_review, cited_subtasks=[])` raises a validation error (existing R4.F5 invariant); the escalate-rescope third path is the only no-cited rejection mechanism. test: `tests/storage/test_holistic_rejection.py::test_bare_rejection_invalid_and_escalate_rescope_path` (cross-reference §1.8.4).
- 2.12.5 — Doc-only reviewer is documented as deferred-to-post-0.4.0 in the skill file's "Out of scope" section; no DB row exists for a `doc-reviewer` agent. file: `src/gobby/install/shared/skills/holistic-review/SKILL.md`.

### 2.13 expansion-qa as a multi-mode verification harness [category: config] (depends: 2.9)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` (extend with a `mode` parameter); `src/gobby/install/shared/prompts/expansion_qa/` (new directory with three prompt files); `src/gobby/build/service.py` (build dispatch reports `expansion_path`); `src/gobby/cli/plans.py` (new CLI invocation surface — see §2.15).

Per the post-#12725 design alignment, the expansion-qa workflow becomes a **configurable verification harness** with three modes selected at invocation time. The agent's tool surface and step structure stay shared; the work it does branches on `mode`. The "expansion-qa" name is preserved for 0.4.0 (semantic baggage acknowledged); rename is deferred to a post-launch dedicated pass.

**Mode selection — driven by upstream context, not by user-side flags:**

- `mode="planning_run_review"` — invoked when a planning post-mortem is requested (always available; not conditional on anything else). Input: round-by-round plan-adversary findings + plan diffs across rounds. Output: per-round classification (`genuine-fix | over-polish | scope-drift | premise-issue`) plus `recommended_stop_round: int` and `premise_issue: bool`.
- `mode="expansion_validation_deterministic"` — invoked when the upstream expansion run took the deterministic parser-driven path (commit `54ad154fb`'s `compile_run` for contract-conforming plans). Validation is structural-only: every `kind: deliverable` section produces exactly one TDD sandwich (TEST/IMPL/REF); `covers:<plan-id>:<section-id>:<item-id>` labels populated; no leaves with `category: planning`; manifest fidelity holds (every acceptance item has at least one `covers:` label, no orphan labels). No LLM judgment needed beyond schema validation.
- `mode="expansion_validation_llm"` — invoked when expansion took the legacy LLM fallback path (ad-hoc task expansion outside the contract; e.g. user typed `/gobby expand` against a non-contract description). Validation includes the deterministic structural checks PLUS LLM judgment over the LLM compiler's output (does it match the user's task description, are the subtasks coherent, do they cover the work).

**`gobby build` reports the expansion path used.** When build dispatches expansion, the lifecycle event written to `task_lifecycle_events` and the operator-facing `BuildResult` (or its CLI/HTTP/MCP equivalent) names which expansion path executed (`deterministic | llm_fallback`) and which expansion-qa mode the validation step will use. This makes path selection observable rather than implicit.

**Acceptance:**

- 2.13.1 — `expansion-qa.yaml` accepts a `mode` parameter (workflow input or step variable) selecting one of `planning_run_review`, `expansion_validation_deterministic`, `expansion_validation_llm`. file: `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`. behavior: invalid mode raises a clear error before the agent loop starts. test: `tests/agents/test_expansion_qa_mode_dispatch.py`.
- 2.13.2 — Three prompt files under `src/gobby/install/shared/prompts/expansion_qa/`: `expansion_validation_deterministic.md`, `expansion_validation_llm.md`, `planning_run_review.md`. file: `src/gobby/install/shared/prompts/expansion_qa/planning_run_review.md`.
- 2.13.3 — Planning-run review prompt produces structured output: `rounds: [{round_number: int, classification: "genuine-fix" | "over-polish" | "scope-drift" | "premise-issue", rationale: str}]` plus `recommended_stop_round: int` and `premise_issue: bool`. file: `src/gobby/install/shared/prompts/expansion_qa/planning_run_review.md`. test: `tests/agents/test_expansion_qa_planning_review.py` validates the JSON shape.
- 2.13.4 — Mode dispatch from `gobby build`: when a build step kicks expansion, the chosen expansion path (`deterministic | llm_fallback`) is captured and surfaced in the `BuildResult` (CLI/HTTP/MCP) and as an event in `task_lifecycle_events`; the downstream validation step is invoked with the matching mode. file: `src/gobby/build/service.py`. behavior: `BuildResult.expansion_path: "deterministic" | "llm_fallback"`. test: `tests/build/test_expansion_path_reporting.py` covers both paths.
- 2.13.5 — Planning-run review invocation surface: `gobby plans review-runs <planning-task-ref>` (CLI; see §2.15) and an MCP tool on `gobby-plans` (§2.15) launch expansion-qa in `planning_run_review` mode against a planning task ID with completed adversary rounds. behavior: launch yields a structured result and persists it on the planning task description under `## Planning-Run Review` heading. file: `src/gobby/cli/plans.py`. test: `tests/cli/test_plans_review_runs.py`.

### 2.14a Hook/rule scoping for autonomous build agents [category: code] (depends: 2.11, 2.12)
`kind: deliverable`

Target: rule definitions under `src/gobby/install/shared/rules/` (existing), hook event handlers under `src/gobby/hooks/` (existing); new module `src/gobby/agents/roles.py` (defines `BUILD_AGENT_NAMES`); rule audit document `src/gobby/install/shared/rules/AGENT_SCOPE_AUDIT.md` (new).

Several rules currently fire on the default agent surface — most notably `require-task-close`, `require-clean-tree-before-status`, `require-task-transitions-skill-loaded`, and the various pre-edit teach-skill nudges. These are designed for **interactive sessions** where a human is in the loop and can satisfy the gates. They are **not appropriate for autonomous gobby-build agents** (qa-reviewer, holistic-reviewer, dev agents in build mode, expansion-qa, merge): build agents need to run uninterrupted to their terminal lifecycle state and use `mark_task_review_*` instead of `close_task` as their terminal action.

**Audit + scope (one-time enumeration baked into the audit document)**:

- Enumerate every rule that fires on default-agent stop / edit / transition events. Source: `src/gobby/install/shared/rules/` plus any installed-DB-only rules surfaced via `list_workflow_definitions(kind="rule")` at audit time.
- Classify each by whether it should fire for autonomous build agents:
  - **Block all build agents**: rule continues to fire only on interactive sessions. Add a session-scope predicate (`session.role == "interactive"` OR `agent_name not in BUILD_AGENT_NAMES`).
  - **Replace with build-mode equivalent**: write a new rule that enforces the build-mode invariant. Example: `require-task-close` becomes `require-mark-task-terminal` for build agents — the agent must call `mark_task_review_approved` OR `mark_task_review_rejected` OR `escalate_task` before terminating; missing call blocks stop the same way.
  - **Keep firing**: rules that apply universally regardless of session role (security blocks, repo-level invariants).

**`mark_task_*` validation behavior**:

- `mark_task_review_approved` and `mark_task_review_rejected` MUST run the **same validation gates** as `close_task` — not skip validation. Currently `close_task` enforces commit-attached, validation_criteria pass, errors_resolved, memory_review_completed (per the task-transitions skill). The build agents hit the same gates when calling `mark_task_*`. There is no `skip_validation` for build-mode terminations.
- Build-mode session variables (`task_claimed`, `errors_resolved`, etc.) are set by the agent's workflow steps the same way they are for interactive sessions. The build-agent yaml shape already sets these; this section verifies nothing slips when the gate moves from `close_task` to `mark_task_*`.

**Acceptance:**

- 2.14a.1 — `BUILD_AGENT_NAMES` constant defined in `src/gobby/agents/roles.py` enumerating every autonomous build agent: `{"qa-reviewer", "holistic-reviewer", "expansion-qa", "merge", "test-architect", "plan-adversary", "plan-author", "backend-developer", "frontend-developer", ...}` (full list per the audit). file: `src/gobby/agents/roles.py`. test: `tests/agents/test_roles.py` covers the contents and freezing.
- 2.14a.2 — Audit document classifies every rule that fires on agent edit/stop/transition events per the table above. file: `src/gobby/install/shared/rules/AGENT_SCOPE_AUDIT.md`.
- 2.14a.3 — `require-task-close`, `require-clean-tree-before-status`, and other interactive-only rules carry a session-role / agent-name predicate so they no longer block build agents. test: `tests/rules/test_build_agent_scope.py` asserts each interactive-only rule does NOT fire when the active session belongs to a `BUILD_AGENT_NAMES` agent.
- 2.14a.4 — Replacement rule `require-mark-task-terminal` blocks autonomous-build-agent stop when no `mark_task_*` or `escalate_task` has been called for the agent's claimed task. file: `src/gobby/install/shared/rules/require_mark_task_terminal.yaml`. test: `tests/rules/test_require_mark_task_terminal.py` covers each build agent and asserts stop is blocked until the terminal call lands.
- 2.14a.5 — `mark_task_review_approved` and `mark_task_review_rejected` run the same validation gates as `close_task` (commit-linked, validation_criteria pass, gate variables set); `skip_validation` is silently stripped when build agents call them. test: `tests/storage/test_mark_task_review_validation.py` (cross-references §1.8.6).

### 2.14b Deprecation pattern for retired agents/orchestrators [category: config]
`kind: deliverable`

Target: new directory `src/gobby/install/shared/workflows/agents/deprecated/`; bundled-sync logic in `src/gobby/workflows/loader.py` (extend); `CLAUDE.md` (update Plan-Coverage / orchestration section to document the new pattern, replacing the tombstone-flag prose).

CLAUDE.md currently describes a tombstone pattern for retired orchestration templates: keep the file in place with `enabled: false, deprecated: true` so bundled-sync stabilizes the existing DB row. The user's preferred pattern going forward is cleaner: **deprecated agents/orchestrators move into a `deprecated/` subdirectory AND bundled-sync removes their DB rows on next startup**. Filesystem location signals deprecation; the DB stays clean.

**Pattern**:

- Move the file from `src/gobby/install/shared/workflows/agents/<name>.yaml` to `src/gobby/install/shared/workflows/agents/deprecated/<name>.yaml`.
- The yaml content keeps as-is (no need to flip `enabled: false`); the path signals deprecation.
- Bundled-sync on startup walks `agents/deprecated/` and ensures any DB row matching a deprecated definition's name is removed. Same shape applies to `workflows/deprecated/`, `pipelines/deprecated/`, `rules/deprecated/` — all reserved for future deprecations; only `agents/deprecated/` is required for this epic.
- Existing tombstoned orchestrator templates (`orchestrator.yaml`, `front-half-orchestrator.yaml`, `dev-orchestrator.yaml`, `delivery-orchestrator.yaml`, plus the old `conductor.yaml` agent) migrate into `deprecated/` as part of this work. Their `enabled: false, deprecated: true` flags are removed from the yaml content (path now signals it).

**Acceptance:**

- 2.14b.1 — New directory `src/gobby/install/shared/workflows/agents/deprecated/` exists; bundled-sync recognizes the subdirectory at startup. file: `src/gobby/install/shared/workflows/agents/deprecated/.gitkeep` (or first deprecated yaml). behavior: walking the directory does not error if the subdirectory is empty.
- 2.14b.2 — Bundled-sync removes DB rows for definitions found in `agents/deprecated/`. file: `src/gobby/workflows/loader.py`. behavior: on next startup after a definition moves to deprecated, the corresponding DB row is deleted; subsequent startups are idempotent (no error when row is already gone). test: `tests/workflows/test_loader_deprecated_dir.py`.
- 2.14b.3 — Migrate existing tombstoned orchestrators (`orchestrator.yaml`, `front-half-orchestrator.yaml`, `dev-orchestrator.yaml`, `delivery-orchestrator.yaml`, old `conductor.yaml`) from their current locations to `agents/deprecated/`; remove the `enabled: false, deprecated: true` flags from their yaml content. file: each of those filenames at the new path under `deprecated/`. test: covered indirectly by `tests/workflows/test_loader_deprecated_dir.py` (counts removed rows).
- 2.14b.4 — `qa-dev.yaml` and old `qa-reviewer.yaml` move to `agents/deprecated/` as part of §2.11.1 / §2.11.2 (this section provides the pattern; §2.11 applies it). behavior: tested as part of §2.11.
- 2.14b.5 — `CLAUDE.md` documents the new `deprecated/` directory pattern, replacing the tombstone-flag description for orchestrators. file: `CLAUDE.md`.

### 2.15 DB-backed plan state + `gobby-plans` MCP/CLI [category: code] (depends: 1.1)
`kind: deliverable`

Target: new SQLite migration `src/gobby/storage/migrations/<next_version>_add_plans_table.py`; new `src/gobby/storage/plans.py` (defines `LocalPlanManager`); new MCP server registered under `src/gobby/mcp_proxy/tools/plans/__init__.py`; new CLI surface `src/gobby/cli/plans.py`; one-shot migration script `scripts/migrate_index_to_plans_table.py`. After migration succeeds, `.gobby/plans/index.yaml` is deleted from the repo.

**Why this exists**: `index.yaml` is a hand-edited file masquerading as system state. It drifts because nothing automated maintains it. Plan state moves into the database; the file system holds only the plan markdown content. `gobby-tasks` is already at 31 tools; folding plan management in would push it past 40+. Plans get their own MCP server, CLI surface, and storage table.

**Schema**:

```sql
CREATE TABLE plans (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    plan_id         TEXT NOT NULL,            -- e.g. "task-12725-lifecycle-dispatch-rev1"
    plan_path       TEXT NOT NULL,            -- relative to repo root
    plan_hash       TEXT,                     -- sha256 of plan file content
    plan_kind       TEXT NOT NULL,            -- 'implementation' | 'strategy'
    state           TEXT NOT NULL,            -- 'active' | 'archived'
    root_task_ref   TEXT NOT NULL,            -- seq_num of parent epic
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    UNIQUE (project_id, plan_id)
);
CREATE INDEX idx_plans_root_task ON plans(root_task_ref);
CREATE INDEX idx_plans_state ON plans(state);
```

Notes on the enum drops vs. the current `index.yaml` shape:

- `plan_kind` drops `legacy` — all plans are contract-conforming after the legacy-strip work; the value is dead.
- `state` drops `merged` (collapses into `archived`); a plan is either active (work in flight) or archived (epic closed, file in `completed/`).

**`gobby-plans` MCP tools** (progressive discovery shape; ~7 tools max):

- `create_plan(plan_id, plan_path, plan_kind, root_task_ref, project=None)` — registers a new plan. Computes `plan_hash` from file content. Default `state=active`. Triggers initial coverage manifest generation (§2.17.4).
- `get_plan(plan_id_or_ref)` — returns row by `plan_id` or `root_task_ref`.
- `list_plans(state=None, plan_kind=None, project=None)` — query.
- `archive_plan(plan_id, reason=None)` — flips `state → archived`, sets `archived_at`, moves the plan file to `.gobby/plans/completed/`, removes the coverage manifest. Atomic per §2.18.
- `update_plan_hash(plan_id)` — recomputes `plan_hash` from current file content; regenerates the coverage manifest if the hash changed (§2.17.3).
- `regenerate_coverage_manifest(plan_id)` — explicit manifest regeneration trigger (operator escape hatch); same logic as the auto-trigger (§2.17.1).
- `delete_plan(plan_id)` — hard-delete (rare; only for genuinely-removed plans authored in error).

**`gobby plans` CLI subcommands** (mirror MCP):

- `gobby plans list [--state STATE] [--kind KIND]`
- `gobby plans show <plan_id>`
- `gobby plans register <plan_path>` (deduces metadata from `## Task Manifest` frontmatter, `root_task_ref` from filename)
- `gobby plans archive <plan_id> [--reason REASON]`
- `gobby plans review-runs <planning-task-ref>` — invokes expansion-qa in `planning_run_review` mode (§2.13.5)

**Migration path**:

- One-shot script `scripts/migrate_index_to_plans_table.py` reads `.gobby/plans/index.yaml`, walks `.gobby/plans/*.md` and `.gobby/plans/completed/*.md`, populates the `plans` table.
- After migration succeeds, `.gobby/plans/index.yaml` is deleted from the repo.
- Existing CI test `tests/plans/test_plan_coverage_ci.py` is rewritten to query the DB instead of reading `index.yaml`.

**Acceptance:**

- 2.15.1 — Migration adds the `plans` table per the schema above. file: `src/gobby/storage/migrations/<next_version>_add_plans_table.py`. test: `tests/storage/test_plans_migration.py` covers schema + indexes.
- 2.15.2 — `LocalPlanManager` in `src/gobby/storage/plans.py` exposes `create_plan`, `get_plan`, `list_plans`, `archive_plan`, `update_plan_hash`, `delete_plan`, `regenerate_coverage_manifest`. file: `src/gobby/storage/plans.py`. test: `tests/storage/test_plans_manager.py` covers each method.
- 2.15.3 — `gobby-plans` MCP server registered and exposes the seven tools above with progressive-discovery list/schema shape. file: `src/gobby/mcp_proxy/tools/plans/__init__.py`. test: `tests/mcp_proxy/test_plans_tools.py` covers tool schemas + happy-path call_tool.
- 2.15.4 — `gobby plans` CLI added under `src/gobby/cli/plans.py` with the five subcommands above. file: `src/gobby/cli/plans.py`. test: `tests/cli/test_plans_cli.py` covers list / show / register / archive / review-runs.
- 2.15.5 — One-shot migration script in `scripts/migrate_index_to_plans_table.py` populates the `plans` table from `index.yaml` + filesystem walk; deletes `index.yaml` after success. behavior: idempotent — re-running on already-migrated state is a no-op (returns existing row counts). file: `scripts/migrate_index_to_plans_table.py`. test: `tests/scripts/test_migrate_index_to_plans_table.py`.
- 2.15.6 — `tests/plans/test_plan_coverage_ci.py` rewritten to query the `plans` table instead of `index.yaml`; `index.yaml` no longer referenced anywhere in `src/` or `tests/`. test: `tests/plans/test_plan_coverage_ci.py` (existing file, modified).

### 2.16 Retire `.grandfathered`, `.grandfathered-task-state.yaml`, `.legacy-classification.yaml` [category: code] (depends: 2.15)
`kind: deliverable`

Target: delete `.gobby/plans/.grandfathered`, `.gobby/plans/.grandfathered-task-state.yaml`, `.gobby/plans/.legacy-classification.yaml`; delete `src/gobby/cli/plan_snapshots.py`; remove `gobby plan grandfathered-refresh` and `gobby plan legacy-classification-refresh` CLI commands from `src/gobby/cli/plan.py`; delete the related test files; update `CLAUDE.md`.

**Why**: `.grandfathered` is dormant ("No grandfathered plans for Epic 1.") and exists for a use case that hasn't materialized. `.legacy-classification.yaml` is dead — all live plans are contract-conforming after the legacy-strip work; nothing's classified as legacy anymore. Both are tech debt: hand-edited or generated-from-stale-state files that nothing automated maintains. If a future need for plan waivers emerges, build it then with DB backing.

**Acceptance:**

- 2.16.1 — Files deleted: `.gobby/plans/.grandfathered`, `.gobby/plans/.grandfathered-task-state.yaml`, `.gobby/plans/.legacy-classification.yaml`. behavior: files no longer exist in the repo (`git ls-files` returns no matches).
- 2.16.2 — `src/gobby/cli/plan_snapshots.py` deleted. file: `src/gobby/cli/plan_snapshots.py` (removed).
- 2.16.3 — CLI commands removed from `src/gobby/cli/plan.py`: `grandfathered_refresh_command`, `legacy_classification_refresh_command`. file: `src/gobby/cli/plan.py`. test: `tests/cli/test_plan_cli.py` updated to remove tests for those commands.
- 2.16.4 — Test files deleted: `tests/plans/test_plan_snapshots_cli.py`, `tests/plans/test_plan_snapshots_hook.py`. behavior: no test references these mechanisms.
- 2.16.5 — `CLAUDE.md` Plan-Coverage Contract section: remove the `.grandfathered` mechanism description and the `.legacy-classification.yaml` requirement; remove the `legacy` value from the documented `plan_kind` enum. file: `CLAUDE.md`.

### 2.17 System-managed coverage manifest lifecycle [category: code] (depends: 2.15)
`kind: deliverable`

Target: extend `LocalPlanManager` (in `src/gobby/storage/plans.py`); coverage manifest path helper consolidates on `coverage_manifest_path(project_id, root_task_ref, plan_id)`.

**Why**: Coverage manifests at `.gobby/plans/coverage/<project_id>/<root_task_ref>/<plan_id>.coverage.yaml` are currently regenerated by hand-run CLI (`gobby plan coverage --regenerate`). Stale manifests linger when plans are archived or renamed. Manifest lifecycle is now DB-driven: regenerate on plan_hash change, remove on plan archive, generate on plan creation.

**Triggers**:

- **On plan creation** (`create_plan` call): generates the initial manifest (rows = acceptance items, all `status: missing` until expansion lands).
- **On plan_hash change** (after plan file edit + commit, then `update_plan_hash` call): recomputes the hash; if it differs from the stored value, regenerates the manifest.
- **On plan archive** (`archive_plan` call): deletes the manifest at the scoped path. Idempotent — missing manifest is a no-op.

**Acceptance:**

- 2.17.1 — `LocalPlanManager.regenerate_coverage_manifest(plan_id)` parses the plan file, regenerates the manifest at `coverage_manifest_path(project_id, root_task_ref, plan_id)` with the current `plan_hash`. behavior: idempotent — existing manifest is overwritten. file: `src/gobby/storage/plans.py`. test: `tests/storage/test_plans_manager.py::test_regenerate_coverage_manifest`.
- 2.17.2 — `archive_plan(plan_id)` deletes the coverage manifest at the scoped path. behavior: missing manifest is a no-op. file: `src/gobby/storage/plans.py`. test: `tests/storage/test_plans_manager.py::test_archive_plan_removes_manifest`.
- 2.17.3 — Auto-trigger on plan_hash change: `update_plan_hash(plan_id)` regenerates the manifest if the hash differs from the stored value. test: `tests/storage/test_plans_manager.py::test_update_plan_hash_regens_manifest`.
- 2.17.4 — `create_plan(plan_id, ...)` generates the initial manifest as part of the create transaction. test: `tests/storage/test_plans_manager.py::test_create_plan_emits_initial_manifest`.
- 2.17.5 — `gobby plan coverage --regenerate` CLI is preserved as a manual escape hatch but documented as "system-managed under normal operation; use only for diagnostic regeneration." file: `src/gobby/cli/plan.py`.

### 2.18 Auto-move plan files on epic terminal state [category: code] (depends: 1.7, 2.15)
`kind: deliverable`

Target: dispatcher action handler for `ArchivePlan` (§1.7); extend `LocalPlanManager.archive_plan` so the file move + DB update + manifest deletion are atomic.

**Why**: Today, when an epic merges, nothing moves the plan file or updates state. A human or agent has to remember. With `archive_plan` as the system-owned API for plan retirement, `rule_archive_plan_on_merge` (§1.7) fires it as a side effect of the merge transition.

**Acceptance:**

- 2.18.1 — `rule_archive_plan_on_merge` (§1.7) calls `LocalPlanManager.archive_plan(plan_id)` when an epic transitions to `lifecycle=merged, status=closed` AND has an associated plan via `task_artifacts.plan_file_path`. test: `tests/dispatch/test_archive_plan_on_merge.py` (cross-references §1.7.5).
- 2.18.2 — `LocalPlanManager.archive_plan(plan_id)` moves the plan file from `.gobby/plans/<plan_id>.md` to `.gobby/plans/completed/<plan_id>.md`, sets `state=archived`, sets `archived_at`, and removes the coverage manifest. behavior: atomic — DB update and file move land together; failure rolls back so the filesystem and DB cannot diverge. file: `src/gobby/storage/plans.py`. test: `tests/storage/test_plans_manager.py::test_archive_plan_moves_file`.
- 2.18.3 — Manual archive via `gobby plans archive <plan_id> --reason ...` performs the same atomic action. file: `src/gobby/cli/plans.py`. test: `tests/cli/test_plans_cli.py::test_archive_command`.
- 2.18.4 — Idempotent on re-archive: archiving an already-archived plan returns the existing row, performs no file movement, raises no error. test: `tests/storage/test_plans_manager.py::test_archive_idempotent`.

### 2.19 Configurable retry caps via BuildConfig + `gobby build` CLI [category: code] (depends: 1.7, 3.2)
`kind: deliverable`

Target: `src/gobby/config/build.py` (BuildConfig field additions); `src/gobby/build/service.py` (BuildOptions extension + resolution logic); `src/gobby/cli/build.py` (CLI flag additions); `src/gobby/dispatch/rules.py` (rules read from runtime config, not hardcoded constants); `src/gobby/storage/tasks/_artifacts.py` (extend if needed to persist resolved caps).

**Why**: Hardcoded retry caps are too rigid. Some builds want aggressive retry (yolo-style "keep trying until it works"); some want strict caps (single-attempt feature work where repeated failure should escalate immediately). Each cap needs:

- A configurable default in the config store (`BuildConfig`).
- A per-build override flag on `gobby build`.
- Plumbing through `BuildOptions` → `BuildResult` → dispatch rules (rules read the resolved value, not a module constant).

**Affected caps**:

| Cap                                         | Default | Config field                          | CLI flag                       |
|---------------------------------------------|---------|---------------------------------------|--------------------------------|
| Expansion attempts before escalate          | 3       | `BuildConfig.max_expansion_attempts`  | `--max-expansion-attempts N`   |
| Per-leaf QA rounds before escalate          | 5       | `BuildConfig.max_qa_rounds`           | `--max-qa-rounds N`            |
| Merge attempts before yolo-fallback / escalate | 3    | `BuildConfig.max_merge_attempts`      | `--max-merge-attempts N`       |
| Holistic review rejection rounds            | 3       | `BuildConfig.max_holistic_rounds`     | `--max-holistic-rounds N`      |
| Plan-adversary rounds (already configurable per /gobby plan max_rounds) | 3 | `BuildConfig.max_review_rounds` | `--max-review-rounds N` (already) |

The plan-adversary `max_review_rounds` flag already exists (per the existing §3.2 and `/gobby plan` skill). This section extends the same pattern across the other caps. No magic numbers in `src/gobby/dispatch/rules.py` — rules read from `BuildOptions` (resolved at build dispatch time and persisted to `task_artifacts` on the parent epic so each tick picks up the correct value).

**Resolution order** (highest precedence first):

1. CLI flag (`--max-qa-rounds 10`).
2. Profile-resolved value (e.g., `--profile review` may set conservative caps; `--profile full-yolo` may bump them).
3. `BuildConfig` default from the config store.
4. Hardcoded fallback constant — used only when the config store is uninitialized (fresh install).

**Acceptance:**

- 2.19.1 — `BuildConfig` adds five fields with sensible defaults: `max_expansion_attempts: int = 3`, `max_qa_rounds: int = 5`, `max_merge_attempts: int = 3`, `max_holistic_rounds: int = 3`, `max_review_rounds: int = 3` (last one may already exist; consolidate). file: `src/gobby/config/build.py`. test: `tests/config/test_build_config.py::test_retry_cap_defaults`.
- 2.19.2 — `BuildOptions` extends with the same fields; defaults pull from `BuildConfig` if not set on the options instance. file: `src/gobby/build/service.py`. test: `tests/build/test_options_resolution.py::test_retry_caps_resolve_from_config_when_unset`.
- 2.19.3 — CLI flags on `gobby build`: `--max-expansion-attempts`, `--max-qa-rounds`, `--max-merge-attempts`, `--max-holistic-rounds` (`--max-review-rounds` already exists). file: `src/gobby/cli/build.py`. test: `tests/cli/test_build_cli_flags.py::test_retry_cap_flags`.
- 2.19.4 — Dispatch rules in `src/gobby/dispatch/rules.py` read retry caps from the parent epic's resolved `BuildOptions` (persisted to `task_artifacts` at build dispatch time) instead of from module-level constants. The hardcoded constants (`MAX_EXPANSION_ATTEMPTS`, `MAX_QA_ROUNDS`, etc.) become fallbacks used only when artifacts are absent. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_retry_caps.py` covers each cap with both default and overridden values.
- 2.19.5 — `BuildResult` includes the resolved retry caps so the operator can see what the build is using. behavior: status output / JSON response shows `max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`, `max_holistic_rounds`, `max_review_rounds`. test: `tests/build/test_result_shape.py::test_retry_caps_in_result`.
- 2.19.6 — Persistence: resolved retry caps written to `task_artifacts` on the parent epic at build dispatch time. behavior: the dispatcher reads from artifacts; subsequent ticks honor the value chosen at build time even if the config store changes mid-build. file: `src/gobby/storage/tasks/_artifacts.py` or `src/gobby/build/service.py` (writes via existing `set_artifacts_atomic`). test: `tests/storage/test_artifacts_retry_caps.py`.

### 2.21 Plan-Coverage Contract: `## Task Manifest` section requirement [category: docs] (depends: 1.1)
`kind: deliverable`

Target: `docs/contracts/plan-coverage.md` (extend); `src/gobby/install/shared/skills/plan-draft/SKILL.md` (extend); `src/gobby/plans/parser.py` (parser updates); `CLAUDE.md` (Plan-Coverage Contract section update).

**Why**: Today, expansion infers the task list from `kind: deliverable` sections. That's section-driven and implicit — the plan author writes prose; the system interprets. The contract makes the task list **explicit** via a `## Task Manifest` section at the end of every implementation plan. The manifest is the single source of truth for what tasks expansion creates; sections become human-readable narrative documenting why each task exists. The manifest is **written by plan-adversary on approval** (§2.22), not by the plan-author — the same agent that just verified the plan is the one writing the canonical task list. Manifest drift is impossible by construction.

**Section shape** (added to the contract):

````markdown
## Task Manifest
`kind: manifest`

```yaml
- title: "Dispatcher mutex"
  category: code
  task_type: feature
  depends_on: ["1.3"]
  validation_criteria: "src/gobby/dispatch/mutex.py exists with acquire-then-act semantics; ..."
  labels: ["covers:task-12725-lifecycle-dispatch-rev1:1.4:1.4.1"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.4"
- ...
```
````

**Contract additions**:

- New `kind` enum value: `manifest`. Exactly one `kind: manifest` section permitted per plan; placed at the end. Other allowed `kind` values stay: `deliverable | framing | verification | deferred`.
- Manifest entry schema: `title` (str), `category` (enum), `task_type` (enum), `depends_on` (list[str], references plan section IDs), `validation_criteria` (str), `labels` (list[str], must include exactly one `covers:` label per acceptance item in the source section), `assigned_agent` (str), `tdd` (bool — implies the deterministic compiler emits a TEST/IMPL/REF triple), `source_section` (str, references a `kind: deliverable` section ID).
- Parser-enforced invariants: every `kind: deliverable` section has exactly one manifest entry referencing it via `source_section`; every `covers:` label resolves to a real acceptance item; no orphan manifest entries (every entry's `source_section` resolves).

**Acceptance:**

- 2.21.1 — `docs/contracts/plan-coverage.md` documents the `## Task Manifest` section, the entry schema, the parser-enforced invariants, and the adversary-writes-on-approval contract. file: `docs/contracts/plan-coverage.md`.
- 2.21.2 — `plan-draft` SKILL.md is updated: plan-author authors narrative sections only, never the manifest. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- 2.21.3 — Parser (`src/gobby/plans/parser.py`) validates the manifest section: schema-checks each entry, enforces the deliverable→manifest-entry 1:1 invariant, resolves every `covers:` label against acceptance items, raises `PlanParseError` on violations. file: `src/gobby/plans/parser.py`. test: `tests/plans/test_parser_manifest.py`.
- 2.21.4 — `CLAUDE.md` Plan-Coverage Contract section updated to mention the manifest. file: `CLAUDE.md`.

### 2.22 plan-adversary agent: manifest emission on approval [category: config] (depends: 2.21)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`; `src/gobby/install/shared/skills/plan-review/SKILL.md`.

**New responsibility**: plan-adversary, in addition to reviewing each round and emitting findings, now writes the `## Task Manifest` YAML in the same call where it approves the plan. The act of writing the manifest forces the adversary to confront ambiguity it might otherwise wave through — if the adversary cannot write a manifest entry for a deliverable, the plan isn't ready.

**Updated workflow per round**:

1. Read plan file + cumulative `## Plan Changelog` (per §2.23 plan-author additions).
2. Review against contract + intent.
3. **If findings**: emit `## Adversary Findings — Round N` to the planning task description; call `mark_task_review_rejected(round_number=N, rejection_notes=...)`. Do NOT edit the plan file. Plan-author handles edits between rounds (§2.23).
4. **If clean**: append `## Task Manifest` YAML to the plan file (per §2.21 schema). Run parser self-check on the manifest. If parser fails, fix the manifest in-place and re-self-check; up to 3 retries before escalating with `escalate_task(reason="needs_human:manifest_emission_failure:<details>")`. On success: `mark_task_review_approved(approval_notes="...; manifest emitted with N entries")`.
5. Always exit fresh — no carryover context to next round (next round gets a new instance per §2.23 fresh-context-per-round contract).

**Tool surface adjustments**:

- Add `Edit` and `Write` permission to plan-adversary's tool surface (currently read-only-ish) — it now writes the manifest section. Edit/Write is **scoped to the plan file path only**; writing to any other path fails. Implementation: a per-step path allowlist enforced via the same `denied_bash_substrings` / `denied_tools` mechanism used elsewhere; the allowlist is computed at agent-spawn time from `task_artifacts.plan_file_path`.
- Add `parse_plan` invocation via the parser library (or via a dedicated MCP tool that wraps the parser) so the adversary can self-check its own manifest output before committing.

**Acceptance:**

- 2.22.1 — `plan-adversary.yaml` review step: on approval (no findings), the agent writes the `## Task Manifest` YAML at the end of the plan file before calling `mark_task_review_approved`. file: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test: `tests/agents/test_plan_adversary_manifest.py` covers the approval-with-manifest path.
- 2.22.2 — `plan-review` SKILL.md updated to document the manifest-emission responsibility. file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- 2.22.3 — Plan-adversary tool surface adds scoped `Edit`/`Write` permission limited to the plan file path. file: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test: writing to any other file path fails the agent's path-allowlist gate.
- 2.22.4 — Adversary self-check: after writing the manifest, invokes parser validation; on failure, retries up to 3 times before escalating with `escalate_task(reason="needs_human:manifest_emission_failure:...")`. test: `tests/agents/test_plan_adversary_self_check.py` covers the parser-failure retry path.
- 2.22.5 — Adversary never edits the plan file when emitting findings (rejection rounds are review-only). test: `tests/agents/test_plan_adversary_no_edits_on_reject.py` confirms no plan-file diff is produced by a rejection round.

### 2.23 plan-author agent / plan-draft skill: fresh context + tighter mandate [category: config] (depends: 2.21, 2.22)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/plan-draft/SKILL.md`; `src/gobby/install/shared/workflows/agents/plan-author.yaml` (if separate yaml exists; if not, document on the spawn-config path that wires plan-draft).

**Why**: Long-context drift across plan-author rounds caused the 26-round case where the agent re-interpreted findings each round and introduced new issues. Two fixes:

1. **Fresh context per round** — every plan-author revision spawns a new agent instance with no carryover from prior author rounds. Input: plan file + the round's adversary findings + a `## Plan Changelog` of prior round summaries. No accumulated session context drifting across rounds.
2. **Tighter mandate** — plan-author's job is "fill holes where the plan is incomplete or inconsistent with the codebase" and "escalate if premise is wrong." It is NOT to redesign or re-engineer in response to adversary findings. Re-engineering belongs in escalation, not in revision.

**Workflow per revision round**:

1. Read plan file + cumulative `## Plan Changelog` + latest `## Adversary Findings — Round N`.
2. Apply surgical fixes: missing acceptance items, ambiguous wording, contradictions with the codebase. Do NOT redesign.
3. Append summary to `## Plan Changelog`: "Round N+1 author: §X.Y — added missing test reference; §A.B — clarified depends_on annotation; ..." (one bullet per surgical fix).
4. Re-route the revised plan through `ExitPlanMode` for user re-approval (interactive mode) or trigger next adversary round automatically (delegated mode).

**Escalation path**: if a finding cannot be addressed by filling a hole — i.e., it requires redesigning a section or rejecting the premise — plan-author calls `escalate_task(reason="needs_human:premise_disagreement:<section_id>:<details>")`. User picks up the plan and decides: revise the requirements, accept the adversary finding, or override.

**Acceptance:**

- 2.23.1 — `plan-draft` SKILL.md updated with the "fresh context per round" requirement and the "fill holes, don't re-engineer; escalate if premise wrong" mandate. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- 2.23.2 — `plan-author` agent (if separate yaml) is spawned with `clean_session=true` (or equivalent fresh-context flag) for every revision round; carries no context from prior rounds. file: `src/gobby/install/shared/workflows/agents/plan-author.yaml` (or the spawn-config path that wires plan-draft).
- 2.23.3 — `## Plan Changelog` section is required on every revised plan post-Round-1; plan-author appends a one-bullet summary per round. behavior: parser tolerates this section under existing `kind: framing`.
- 2.23.4 — Plan-author escalates on premise disagreement instead of re-engineering. test: `tests/agents/test_plan_author_escalation.py` covers a contrived "adversary suggests redesign" case; plan-author escalates rather than complies.

### 2.24 `/gobby plan` skill: end-to-end coordinator flow [category: config] (depends: 2.21, 2.22, 2.23)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/plan/SKILL.md` — substantial rewrite.

**Why**: The current `/gobby plan` skill has the right shape (opt-in mode, adversarial loop, terminal cleanup) but is wired around the old design (combined plan-author/adversary, no manifest emission, no fresh-context-per-round, no delegated-flow handoff to build). This rewrite encodes the canonical end-to-end flow. **If anything below surprises the user when they read it, alignment is broken and the surprise is itself a finding.**

**Canonical flow**:

**Phase 1 — Initial plan authoring (Claude + user, interactive)**

1. User invokes `/gobby plan` in chat (or `/gobby plan <topic>`).
2. Skill loads `plan-draft` (and any related skills) via `gobby-skills:get_skill`.
3. Claude collaborates with the user to draft the plan: requirements gathering, structure, contract-formatting per `plan-draft`. No spawned agents yet — Claude IS the plan-author for the first draft.
4. Claude presents the draft via native `ExitPlanMode`.
5. User approves the first draft. Rejections at this stage loop back to step 3 with the user driving revisions; no adversary involvement yet.

**Phase 2 — Mode selection (post-first-draft-approval)**

6. Skill prompts the user via `AskUserQuestion`: **Interactive** (per-round adversary findings shown to user; user re-approves each revised plan) or **Delegated** (silent revision loop until terminal; user only sees terminal outcome). Also prompts for `max_rounds` (default = `BuildConfig.max_review_rounds`, configurable per §2.19).

**Phase 3a — Adversarial review loop (Claude as coordinator)**

For each round N (user-facing 1-indexed; internally 0-indexed per existing convention):

7. **Round 1**: spawn `plan-adversary` (LLM B, fresh context) against the user-approved first draft. No plan-author involvement on Round 1 — the user authored the draft.
8. **Round N (N > 1)**: spawn `plan-author` (LLM A, fresh context per §2.23) with: current plan file + cumulative `## Plan Changelog` + `## Adversary Findings — Round N-1`. Plan-author applies surgical fixes per §2.23 mandate (fill holes, don't re-engineer); appends a one-bullet round summary to `## Plan Changelog`; exits. Then spawn `plan-adversary` (LLM B, fresh context) against the revised plan.
9. **Adversary outcomes** (every round):
   - **Approve** → adversary writes `## Task Manifest` YAML to plan file (per §2.22), self-checks via parser, calls `mark_task_review_approved`. Skill advances to **Phase 4**.
   - **Reject** → adversary writes `## Adversary Findings — Round N` to planning task description, calls `mark_task_review_rejected`. Skill: in interactive mode, presents findings to user, re-prompts plan via `ExitPlanMode` for re-approval before next round; in delegated mode, silently advances to next round.
10. **Round-budget exhaustion** (rejection on Round = max_rounds): skill presents the final findings + offers terminal options (revise manually + run `gobby build` directly, abort + close planning epic, restart with fresh budget). Each terminal option runs cleanup.
11. **Coordinator role**: Claude (this skill instance) does NOT review or revise the plan during Phase 3. Its job is purely orchestration — spawn agents, wait on durable wake signals from the daemon (per the spawn-then-end-turn pattern), interpret terminal task states, route to the next step.

**Phase 3b — Delegated build handoff (alternative flow only when user picked "Delegated" in Phase 2)**

This phase fires after the interactive review loop terminates (approval or exhaustion). It does NOT replace Phase 3a — Phase 3b is a follow-on once the plan is approved-or-final.

12. Skill loads the `build` skill via `gobby-skills:get_skill`.
13. Skill asks the user via `AskUserQuestion`: what scope of build do you want?
    - **Plan only** — adversary already approved; no expansion fires. User runs expansion later.
    - **Plan + test_arch** — run plan-adversary (already done) → test architect (per existing `rule_test_arch`).
    - **Plan + test_arch + expand** — through expansion only; user runs dev/qa/holistic later.
    - **Plan + full build** (test_arch → expand → dev → qa → holistic → pr → merge) — the killer-feature path.
14. Based on the choice, the skill EITHER:
    - Triggers `gobby build <plan_file> --profile <resolved>` in this session (immediate execution), OR
    - Provides the CLI command to the user as a string for them to run at their convenience.
15. Skill exits.

**Phase 4 — Expansion handoff (post-adversary-approval, regardless of interactive vs. delegated)**

16. Skill calls `start_expansion_run(task_id=plan_parent_ref, plan_file=artifact_path, auto_apply=true)`.
17. Wait for completion via durable daemon wake.
18. On success: report child-task count to user; close planning epic; run terminal cleanup.
19. On failure: surface error; offer retry / retry-with-overrides / escalate per existing skill design.

**Acceptance:**

- 2.24.1 — `plan/SKILL.md` rewritten to encode the canonical flow above. Phases, step numbering, agent spawn shapes, and terminal cleanup paths match this document. file: `src/gobby/install/shared/skills/plan/SKILL.md`. test: `tests/skills/test_plan_skill_flow.py` exercises each phase via mocked spawn / completion notifications.
- 2.24.2 — Round-1 adversary spawn is against the user-approved first draft, no plan-author involvement. test: `tests/skills/test_plan_skill_flow.py::test_round_1_no_plan_author`.
- 2.24.3 — Round-N (N > 1) spawns plan-author with fresh context first, then plan-adversary against the revised plan. Plan-author input includes cumulative `## Plan Changelog` + latest `## Adversary Findings`. test: `tests/skills/test_plan_skill_flow.py::test_round_n_spawns_author_then_adversary`.
- 2.24.4 — On adversary approval, manifest emission is the trigger for expansion handoff. behavior: skill calls `start_expansion_run` only after manifest YAML is present in plan file. test: `tests/skills/test_plan_skill_flow.py::test_expansion_after_manifest`.
- 2.24.5 — Delegated build handoff (Phase 3b) loads `build` skill, prompts for scope, dispatches or hands back CLI. file: `src/gobby/install/shared/skills/plan/SKILL.md`. test: `tests/skills/test_plan_skill_flow.py::test_delegated_build_handoff`.
- 2.24.6 — Coordinator role enforced: skill does NOT edit the plan file during Phase 3. test: `tests/skills/test_plan_skill_flow.py::test_no_edits_during_review_loop` asserts no Edit/Write tool calls fire from this skill's session during Phase 3 rounds.

### 2.20 Re-expansion of #12725 as Epic 1 end-to-end validation [category: manual] (depends: 2.11, 2.12, 2.13, 2.14a, 2.14b, 2.15, 2.16, 2.17, 2.18, 2.19, 2.21, 2.22, 2.23, 2.24)
`kind: deliverable`

Target: live execution against the running daemon — re-expand `#12725` against the revised `task-12725-lifecycle-dispatch-rev1.md` once plan-adversary approves it; verify the deterministic compile path produces a covered manifest.

**Why this is a deliverable, not a verification afterthought**: Epic 1 (#13175, the Plan-Coverage Contract) was built so that contract-conforming plans could be expanded mechanically into task trees with auto-populated `covers:` labels. The contract has never been exercised end-to-end on a real-world plan with the new design. This Epic-2 plan is the test case: it includes everything from typed sections to acceptance items to phase boundaries to dependency annotations, and re-expanding it under the deterministic compile path is the validation that Epic 1's foundation actually works. Without this validation, we don't know whether Epic 1 shipped a working contract or just a structurally-compliant one.

**Pre-conditions**:

- All previous §2.X deliverables are at minimum **specified in the plan** (acceptance items reviewed by plan-adversary). They do not all need to be implemented before re-expansion fires — re-expansion creates the leaves under which the implementation work happens.
- `task-12725-lifecycle-dispatch-rev1.md` has its `plan_hash` up to date and a fresh coverage manifest at `.gobby/plans/coverage/<project_id>/12725/task-12725-lifecycle-dispatch-rev1.coverage.yaml`.
- The deterministic compile path from commit `54ad154fb` is live in the running daemon.

**Procedure**:

1. Reopen `#12725` if closed.
2. Trigger expansion: `call_tool("gobby-tasks-ops", "start_expansion_run", {"task_id": "#12725", "plan_file": ".gobby/plans/task-12725-lifecycle-dispatch-rev1.md", "auto_apply": true, "force_new": true})`.
3. Wait for the run to reach `status=completed` via the daemon's completion notification.
4. Inspect the compiled task tree:
   - Every `kind: deliverable` section in the plan (≥18 after this revision) produces a TDD sandwich (TEST/IMPL/REF) under the parent epic.
   - Each generated leaf carries `covers:task-12725-lifecycle-dispatch-rev1:<section-id>:<acceptance-item-id>` labels.
   - Phase grouping into sub-epics matches `## P1`, `## P2`, `## P3`.
   - Dependencies (`depends: 1.3, 1.4`) translated to task-graph edges.
   - `assigned_agent` populated on every automated-category leaf.
5. Regenerate the coverage manifest against the new task tree; confirm every row flips from `status: missing` → `status: covered`.

**Acceptance:**

- 2.20.1 — Re-expansion of #12725 against `task-12725-lifecycle-dispatch-rev1.md` succeeds (`status=completed`) under the deterministic compile path. behavior: the run's `compiled_summary` reports correct phase_count, task_count, and dependency_count; logs name `deterministic` as the path used.
- 2.20.2 — Every deliverable section in the plan produces TDD-sandwich leaves under the correct phase sub-epic. test: `tests/build/test_compile_12725_e2e.py` (golden-fixture comparison or DB inspection after live run).
- 2.20.3 — Every leaf carries `covers:<plan-id>:<section-id>:<acceptance-item-id>` labels for every acceptance item in its source section. test: same as 2.20.2.
- 2.20.4 — Coverage manifest after re-expansion has every row at `status: covered` (zero `missing`, zero `invalid`). test: `tests/build/test_manifest_after_expansion.py` (or a one-shot gate in the existing `tests/plans/test_plan_coverage_ci.py`, post-§2.15 rewrite).
- 2.20.5 — `gobby build` status output for the re-expansion names `expansion_path: deterministic` (per §2.13.4). behavior: observable in the BuildResult / status JSON.

If re-expansion fails or produces an incomplete tree, the failure modes inform Epic 1 follow-up work — that is itself valuable signal. Epic 1 does not get to ship as "done" until #12725 round-trips through it cleanly.

## P3 Phase 3: Entry Points — Build Config + CLI + Interactive Skill
`kind: framing`

**Goal**: Provide the "one-line starts the automation" surface. Config hierarchy with global / project / flag / task layers. A CLI command for the quick path and an interactive `/gobby build` skill for the wizard path.

### 3.2 Build service — CLI + MCP + HTTP shared core [category: code] (depends: 3.1)
`kind: deliverable`

> **Status: partial** — CLI/MCP/HTTP shared core shipped via commit `614c5bbe` at `src/gobby/build/service.py`. `_kick_dispatcher_tick()` is an explicit placeholder returning 0 (intentional; gets wired in §1.9 once the dispatcher exists). `target_branch=None → current git HEAD` resolution before persisting artifacts is not yet implemented.

Target: shared core at `src/gobby/build/service.py` (new package); three thin surfaces.

All three surfaces (`gobby build ...`, MCP `call_tool("gobby-tasks-ops", "build_task", ...)`, HTTP `POST /api/build`) are thin wrappers over a single canonical function:

```python
# src/gobby/build/service.py
from dataclasses import dataclass
from gobby.config.build import Isolation

@dataclass
class BuildOptions:
    skip_stages: list[str]         # resolved; empty if none
    isolation: Isolation           # "none" | "worktree" | "clone"  (R4.F2: all three wired)
    yolo: bool
    max_review_rounds: int
    target_branch: str | None = None   # R4.F6: None = resolve to current git HEAD at service-call time
    assigned_agent: str | None = None  # for single-leaf builds via --agent

@dataclass
class BuildResult:
    task_id: str
    created: bool                   # True if new epic was created
    initial_lifecycle: str
    applied_stages_skipped: list[str]
    tick_dispatched: int            # how many tasks the first tick dispatched

async def build(input_ref: str, opts: BuildOptions,
                 db, project_id: str) -> BuildResult:
    """Canonical build function. Called by CLI/MCP/HTTP surfaces identically."""
    ...
```

**Surface wrappers:**

- `src/gobby/cli/build.py` — parses argv → `BuildOptions` → calls `service.build(...)` → prints `BuildResult`.
- `src/gobby/mcp_proxy/tools/build.py` (new MCP tool, registered on gobby-tasks-ops) — JSON schema → `BuildOptions` → calls service → returns result dict.
- `src/gobby/servers/routes/build.py` — `POST /api/build` — JSON body → `BuildOptions` → calls service → returns JSON. Web UI consumer.

**CLI signature:**

```bash
gobby build <plan_file>
gobby build <#taskref>
gobby build                               # interactive — invokes /gobby build skill
```

**CLI flags:**

```
--profile quick|review|full|full-yolo|auto   # sugar; expands at parse time
--skip-stage a,b,c                            # comma-separated; adds to resolved skip set
--isolation none|worktree|clone               # default: worktree (or profile-specified). R4.F2: all three implemented (clone uses CloneGitManager, §1.6/§1.7/§2.10).
--yolo / --no-yolo                            # overrides profile's yolo bool
--max-review-rounds <N>                       # for plan_review stage
--target-branch <name>                        # R4.F6: base branch for worktree/merge; default = current git HEAD
--agent <name>                                # only for single-leaf inputs; sets assigned_agent
```

**Validation:**

- Any `--skip-stage` entry not in `SKIPPABLE_STAGES` raises a clear error listing the valid set.
- `--isolation clone` is fully supported (R4.F2). The build service validates that `BuildConfig.clones_dir` is writable at invocation; missing/unwritable raises a clear error. The dispatcher creates the clone via `CloneGitManager.create_clone` (§1.6/§1.7/§1.9), records it in `LocalCloneManager`, and persists `clone_path` in `task_artifacts`.
- `--agent` on a non-leaf input raises (agent-assignment is per-leaf and normally set by expansion).
- `--profile auto` resolves via `resolve_profile(cfg, "auto", input_ref)`.
- **R4.F3**: `--profile quick` on a plan-file input raises ("quick profile requires a leaf task ref; for plan files use review or full"). Quick + plan-file would otherwise dispatch the planning epic itself as a leaf, but `rule_dispatch_leaf` excludes `category=planning` per `AUTOMATED_LEAF_CATEGORIES` (§1.7). `auto` already maps plan-file → review, so this constraint only bites explicit `--profile quick` on plan-file inputs.
- **R4.F6**: `--target-branch` is validated against `git branch --list <name>` at build time; missing branch raises with the available-branches list.
- **R6.F2** (mutual-exclusion enforcement): non-`none` isolation is rejected on single-leaf builds. `build <#leafref> --isolation worktree` (or `clone`) raises ("isolation requires an epic; leaf builds inherit their parent's isolation or use `none`"). For `build <#taskref>` on an epic, isolation can only be set if the epic does NOT yet have a `worktree_path` or `clone_path` populated; attempting to change isolation on a built epic raises ("epic already has <existing-isolation> artifact at <path>; tear down with the appropriate cleanup tool before rebuilding"). The schema-level CHECK constraint (§1.1b) is the last-line backstop, but the build service rejects the request before any DB write to give a clear error.

**Canonical build behavior:**

- `build <plan_file>`: create planning root epic (task_type=epic, category=planning) with `plan_file_path`, `target_branch` (from `--target-branch` or `git rev-parse --abbrev-ref HEAD`) in `task_artifacts`, `allow_automation=true`, `lifecycle` set to the first enabled stage from {`plan_review`, `test_arch`, `expanding`, `in_development`}, `isolation` set, `yolo` set. Apply `stage-:*` labels for every skipped stage. Record a seed event in `task_lifecycle_events` (reason="gobby build", by_actor="cli|mcp|http"). Kick a dispatcher tick.
- `build <#taskref>` on an epic: set `allow_automation=true` on the epic, populate/refresh `target_branch` in `task_artifacts`, cascade the resolved `isolation`, `yolo`, and `stage-:*` labels to the entire subtree (see §3.4). Set `lifecycle` based on what state the epic is in (if `plan_file_path` present and `plan_review` not skipped → `plan_review`; else skip ahead). Kick a tick.
- `build <#leafref>` on a single leaf: validate `task.category in AUTOMATED_LEAF_CATEGORIES` (R4.F3 — leaves with `category=planning` are rejected). **Force `isolation=none` for solo-leaf builds** (R6.F2 — leaves don't own isolation artifacts; `--isolation worktree` or `clone` raises). Set `allow_automation=true` and `assigned_agent` (from `--agent` or `expansion-agent-selection` heuristic). Set `lifecycle=in_development` (all pre-dev stages skipped for a solo leaf). Kick a tick. This covers the quick-profile single-task flow cleanly without any stack logic (R3.F2 subsumed). For solo-leaf builds, no `target_branch` is captured in `task_artifacts` because there is no isolation artifact to merge back; the leaf works in-branch on the source repo's current HEAD.
- `build` (no args): invoke `/gobby build` skill interactively (§3.3).

CLI tick-kick is a single explicit call to the state-dispatcher handler; the periodic cron fires every interval regardless.

**Acceptance:**

- 3.2.1 — Shared build service validates plan-file, epic, and leaf inputs before writing build state. file: `src/gobby/build/service.py`.
- 3.2.2 — CLI build surface delegates to the shared build service. file: `src/gobby/cli/build.py`.
- 3.2.3 — MCP build surface delegates to the shared build service. file: `src/gobby/mcp_proxy/tools/build.py`.
- 3.2.4 — HTTP build route delegates to the shared build service. file: `src/gobby/servers/routes/build.py`.
- 3.2.5 — `_kick_dispatcher_tick()` triggers an immediate dispatcher heartbeat after build-state writes (replaces the current placeholder that returns `0`); the periodic cron continues to fire independently. symbol: `gobby.build.service._kick_dispatcher_tick`. behavior: returns the count of tasks dispatched by the kicked tick (the same shape `BuildResult.tick_dispatched` exposes).
- 3.2.6 — `BuildOptions.target_branch=None` resolves to `git rev-parse --abbrev-ref HEAD` at service-call time before any `task_artifacts` write; explicit `--target-branch <name>` is preserved as-is. behavior: `task_artifacts.target_branch` is never persisted as `None` for plan-file or epic builds. file: `src/gobby/build/service.py`. test: `tests/build/test_target_branch.py` covers the default-resolve path and the explicit-override path.

## V1 Verification
`kind: verification`

**Unit tests** (write during expansion — TDD sandwiches auto-generated):

- `tests/dispatch/test_mutex.py` — acquire/release against `task_dispatch_mutex` table, TTL expiry, spawn-detach semantics, **R3.F5**: release on scope exit when not detached regardless of kind; startup sweep; concurrent acquires exactly-one wins.
- `tests/dispatch/test_rules.py` — table-driven per rule: fabricate `(lifecycle, status, labels, task_type, allow_automation, yolo, isolation, assigned_agent, category, task_artifacts)` hitting + missing. Specific coverage:
  - `_stage_enabled` reads `stage-:*` labels only (no STAGE_BY_PROFILE).
  - `rule_dispatch_leaf` (R4.F3) for each `AUTOMATED_LEAF_CATEGORIES` member spawns with `assigned_agent`; `category=planning` is a no-op (rule does not match).
  - `rule_dispatch_leaf` with `assigned_agent="frontend-developer"` spawns frontend-developer.
  - `rule_dispatch_leaf` with `assigned_agent=None` spawns backend-developer + AppendAuditMarker (never escalates); marker text includes the leaf's category.
  - `rule_pr` with `yolo=false` escalates; with `yolo=true` emits `AppendAuditMarker + AdvanceLifecycle(merging)`.
  - `rule_plan_adversary` rounds-exhausted path: non-yolo escalates; yolo force-approves.
  - `rule_merging` passes `yolo` as an initial variable to the merge agent.
  - `rule_create_worktree` (R4.F6) reads `task_artifacts.target_branch`; falls back to `git rev-parse --abbrev-ref HEAD` when artifact absent; passes resolved branch to `CreateWorktree.base_branch`. Fires only for `isolation=worktree`.
  - `rule_create_clone` (R4.F2) fires only for `isolation=clone`; emits `CreateClone(epic_task_id, base_branch=<target>)`; no-ops when `clone_path` already set; respects `_has_isolation_artifact` mutual exclusion with worktree.
- `tests/dispatch/test_rule_expansion.py` (R4.F1) — covers the `rule_start_expansion` / `rule_validate_expansion` split:
  - `rule_start_expansion` emits `StartExpansionRun` when `expansion_run_id` is NULL and attempts < cap.
  - `rule_start_expansion` no-ops while `expansion_run_id` is set and run is not terminal.
  - `rule_validate_expansion` spawns `expansion-qa` once `expansion_run_id` is set and the run state is `completed`; no-op when `claimed_by_session_id` is set (candidate scan filters during active QA).
  - On expansion-qa rejection (mocked via `mark_task_review_rejected`), `expansion_run_id` clears and `expansion_attempts` increments; next tick `rule_start_expansion` re-fires.
  - At `expansion_attempts >= MAX_EXPANSION_ATTEMPTS`, non-yolo emits `EscalateTask`; yolo emits `AppendAuditMarker + AdvanceLifecycle(in_development)`.
- `tests/tasks/test_expansion_agent_selection.py` — seed a plan with a mix of FE/BE/ambiguous `### N.N` sections; run expansion with mocked LLM output against a mocked `list_agent_definitions`; assert every code leaf has `assigned_agent` populated; assert ambiguous leaves default to `backend-developer` and produce an `## Agent Selection` description marker.
- `tests/tasks/test_expansion_qa_transitions.py` (R3.F3) — expansion-qa calls `mark_task_review_approved` on success and `mark_task_review_rejected` on missing `assigned_agent`; fabricate both paths and assert the parent epic's lifecycle/status moves correctly.
- `tests/dispatch/test_dispatcher.py` — end-to-end tick with mocked `execute_spawn`; assert TOCTOU re-evaluation under lock; assert agent-slot cap honored; assert `TickReport` is persisted to `~/.gobby/logs/dispatcher.jsonl` as structured JSON.
- `tests/dispatch/test_cron_registration.py` — handler registered under name "state-dispatcher"; cron row inserted idempotently.
- `tests/storage/test_transitions_lifecycle.py` — extended review tools advance lifecycle at the right boundaries; planner-resubmit clears rejection marker; **R3.F4**: holistic approval advances `holistic_review → pr`; status reset on advance (review_approved → open); `advance_lifecycle(reason, by_actor)` writes a `task_lifecycle_events` row; `de_escalate_task(lifecycle=...)` is a single-call recovery that updates both status and lifecycle in one transaction.
- `tests/storage/test_holistic_rejection.py` (R4.F5) — `mark_task_review_rejected(epic, cited_subtasks=[...])` on `lifecycle=holistic_review` atomically appends findings, reopens cited subtasks (`(holistic_review, review_approved | closed) → (in_development, open)`), and rewinds the epic lifecycle (`holistic_review → in_development`) in a single transaction; assert atomicity by injecting a mid-transaction failure and checking nothing partially applied. Rejecting `lifecycle=holistic_review` without `cited_subtasks` (or with `[]`) raises a validation error. Confirm `rule_all_leaves_holistic` (renamed from `rule_all_closed_advance_to_holistic`, §1.7) does NOT immediately re-fire because at least one cited leaf is now `open` at `lifecycle=in_development`. Cover the escalate-rescope third path: `escalate_task(epic, reason="needs_human:rescope_required:...")` flips the epic to `status=escalated`, leaves `lifecycle=holistic_review` unchanged.
- `tests/storage/test_expansion_rejection.py` (R4.F1) — `mark_task_review_rejected` on `lifecycle=expanding` clears `task_artifacts.expansion_run_id` and increments `expansion_attempts` in a single transaction; `mark_task_review_approved` on `lifecycle=expanding` does NOT clear those fields (audit trail of which run produced the approved tree).
- `tests/build/test_target_branch.py` (R4.F6) — `gobby build` captures `git rev-parse --abbrev-ref HEAD` by default; `--target-branch <name>` overrides; missing branch raises with the available-branches list; `task_artifacts.target_branch` is populated on plan-file/epic/leaf builds; `rule_create_worktree` consumes it; legacy task without artifact row falls back to current HEAD.
- `tests/dispatch/test_clone_dispatch.py` (R4.F2 in-scope, R6.F1 grounded) — `_dispatch` for `CreateClone` invokes `CloneIsolationHandler.prepare_environment(SpawnConfig(...))` (the high-level API that composes `LocalCloneManager.create` + `CloneGitManager.create_clone` + bootstrap); persists both `clone_path` and `clone_id` into `task_artifacts` via a single `set_artifacts_atomic` call; the SQL CHECK constraint blocks the write if a worktree artifact already exists. `_resolve_cwd(leaf, agent_name="developer")` routes into the clone path; `_resolve_cwd(epic, agent_name="merge")` returns repo root.
- `tests/dispatch/test_merge_integration.py` (R4.F7 + R6.F3) — exercises §2.10 with the existing tool-driven contract:
  - Clean worktree merge: `merge_worktree(worktree_id, push=true)` returns success → agent calls `mark_task_review_approved(approval_notes=...)`, `mark_worktree_merged(worktree_id)`, `delete_worktree(worktree_id)`, `clear_isolation_pair(epic_task_id, "worktree")`; lifecycle advances to `merged`, status `closed`. Merge SHA capture deferred to #12728 (R7.F3) — `merge_commit_sha` stays NULL.
  - Clean clone merge: `sync_clone(clone_id)` then `merge_clone(clone_id, target_branch)` returns success → same lifecycle path; clone deleted via `delete_clone` and artifacts cleared via `set_artifacts_atomic(clone_path=None, clone_id=None)` iff `cleanup_clones_on_merge=true`.
  - Worktree conflict resolved by AI: `merge_worktree` returns has_conflicts=true → AI flow (`merge_start` → `merge_resolve` → `merge_apply`) resolves → `merge_worktree(push=true)` succeeds → success path.
  - Worktree conflict, AI fails (non-yolo): `merge_abort` then `escalate_task` with `de_escalate_task` instructions in reason; lifecycle stays `merging`, status `escalated`.
  - Yolo conflict, single attempt fails: `merge-attempts:1` label applied; `mark_task_review_rejected` called on `lifecycle=merging` (R6.F4 case); status resets to `open`; lifecycle stays `merging`. Next tick re-dispatches.
  - Yolo conflict, retries exhausted (`merge-attempts:N >= cap`): `gobby-tasks-ops:append_description_section(heading="Yolo Fallbacks", body=...)` + `mark_task_review_approved(approval_notes=...)`; lifecycle advances to `merged`; isolation artifact pair NOT cleared (preserved for inspection). No `escalate_task` ever called.
- `tests/storage/test_artifact_xor.py` (R6.F2 + R7.F4) — SQL CHECK enforces all three predicates: pairwise co-presence within each isolation family, family XOR, plus rejecting partial states (`worktree_path` set with `worktree_id` NULL or vice versa). `set_artifacts_atomic` raises with a clear error mapping the failing predicate; `clear_isolation_pair("worktree")` clears the worktree pair atomically and lets a subsequent clone-pair write succeed.
- `tests/mcp_proxy/test_tasks_ops_artifacts.py` (R7.F3 / §1.1d) — covers the new MCP tools: `set_artifact` with each valid field; `set_artifact` with an invalid field name surfaces an allowlist error; `set_artifacts_atomic` writes a `(worktree_path, worktree_id)` pair atomically; `clear_isolation_pair("clone")` clears `clone_path` + `clone_id` together; `append_description_section` appends a `## {heading}\n{body}\n` block and is idempotent on duplicate `(heading, body)` calls within the same transaction; `get_artifacts` returns the row dict or empty when absent.
- `tests/build/test_isolation_validation.py` (R6.F2) — `build <#leafref> --isolation worktree` raises with leaf-isolation-rejected message; `build <#taskref> --isolation clone` on an epic that already has `worktree_path` raises with the change-isolation-rejected message; `build <#taskref>` on a not-yet-built epic accepts isolation cleanly.
- `tests/storage/test_merging_rejection.py` (R6.F4) — `mark_task_review_rejected` on `lifecycle=merging` leaves lifecycle unchanged, resets status to `open`, appends findings, writes a `task_lifecycle_events` row. `cited_subtasks` ignored on this lifecycle.
- `tests/tasks/test_expansion_start_run.py` (R6.F5) — `ExpansionService.start_run(task_id, tdd=True)` creates an expansion-run row, calls `LocalExpansionRunManager.start`, kicks off `compile_run`, and returns the persisted `ExpansionRun` with a populated `id`. Failure during `compile_run` flips `run.status` to `failed` but the row is still returned (so the dispatcher's `set_artifact("expansion_run_id", run.id)` write still happens; expansion-qa picks up failed runs and rejects in §2.9).
- `tests/agents/test_merge_integration.py` (R4.F7) — exercises the §2.10 finalize step:
  - Clean merge → `mark_task_review_approved(epic, approval_notes=...)` called; `mark_worktree_merged` + `delete_worktree` + `clear_isolation_pair` for worktree (or `delete_clone` + `clear_isolation_pair` for clone); lifecycle advances to `merged`, status `closed`. `merge_commit_sha` stays NULL (deferred to #12728 per R7.F3).
  - Non-yolo conflict → `escalate_task` called with reason starting `needs_human:`; reason text includes `de_escalate_task(... lifecycle=Lifecycle.merged ...)` instructions for the human.
  - Yolo conflict, single-attempt failure → `mark_task_review_rejected` called; `merge-attempts:1` label applied; lifecycle stays `merging, status=open`.
  - Yolo conflict, retries exhausted (`merge-attempts:N >= cap`) → `append_description_section(heading="Yolo Fallbacks", ...)` + `mark_task_review_approved(epic, approval_notes="yolo: merge attempts exhausted, force-advanced; isolation artifact preserved")`; lifecycle advances to `merged`; isolation pair NOT cleared (preserved); no escalate ever called.
- `tests/storage/test_is_blocked_by_deps.py` — escalated upstream blocks; `review_approved` upstream blocks transiently; `closed` upstream unblocks; `suggest_next_task` / `list_ready_tasks` / `list_blocked_tasks` all use the centralized predicate.
- `tests/build/test_service.py` — canonical `build(input_ref, opts)` covers plan_file, epic taskref, leaf taskref inputs; cascades `isolation`, `yolo`, `stage-:*` labels to the subtree; validates `--skip-stage` against SKIPPABLE_STAGES; `--agent` on non-leaf input raises.
- `tests/build/test_surfaces.py` — CLI, MCP tool, and HTTP endpoint all produce identical `BuildResult` for equivalent inputs (shared-core invariant).

**Integration**:

- `tests/dispatch/test_end_to_end_full.py` — seed a small epic with `lifecycle=plan_review, allow_automation=true, isolation=worktree, yolo=false`, no skip-stages; run dispatcher ticks in a loop with mocked agent runs; assert lifecycle advances through `plan_review → test_arch → expanding → in_development → holistic_review → pr → merging → merged` without double-dispatch; assert `task_lifecycle_events` has exactly one row per transition.
- `tests/dispatch/test_end_to_end_full_yolo.py` — same setup with `yolo=true` and `stage-:pr`; assert `rule_pr` skips straight to `merging` with an `AppendAuditMarker`; terminal state reached without any escalation.
- `tests/dispatch/test_quick_profile_leaf.py` — single leaf task built via `gobby build <#leaf> --profile quick --agent backend-developer`; assert the leaf reaches `lifecycle=in_development` directly, `rule_dispatch_leaf` dispatches `backend-developer`, agent closes the leaf, no `rule_qa` fires. Also assert that `--profile quick` on a plan-file input raises (R4.F3).
- `tests/dispatch/test_crash_recovery.py` — simulate daemon restart mid-dispatch; assert startup sweep clears stale non-spawn mutexes in `task_dispatch_mutex` and preserves spawn-kind mutex whose `running_agents` row is still active; R3.F5 specific: simulate spawn failure before detach → assert lease is released on scope exit.

**Manual smoke**:

1. `uv run gobby start --verbose`
2. `uv run gobby build .gobby/plans/task-12725-lifecycle-dispatch.md --profile full --max-review-rounds 2 --target-branch main`
3. Observe cron ticks in logs; watch the task progress through lifecycle stages via `gobby tasks show #<id>` (check `task_lifecycle_events` for transition history).
4. Kick a quick-profile single-leaf task: create a bug via MCP with description+validation_criteria; `uv run gobby build #<id> --profile quick --agent backend-developer`; watch it dispatch and close.
5. Verify web UI: open the build page, submit the equivalent of step 2 via the UI; confirm the POST `/api/build` endpoint returns a `BuildResult` identical to the CLI call.

**Lint / type / regression**:

- `uv run ruff check src/ && uv run mypy src/`
- `uv run pytest tests/dispatch/ tests/storage/test_transitions_lifecycle.py -v`
- Spot-run `tests/scheduler/`, `tests/agents/spawn/`, `tests/workflows/sync/` to confirm no regressions in the surfaces we touched.

## O1 Out of Scope (filed as follow-ups)
`kind: framing`

- **#12728** — PR-creation and merge skill with AI conflict resolution. Two distinct capabilities landing together: (1) PR-creation agent (opens GitHub PR with description, links, labels) — replaces the `EscalateTask` in rule 11; (2) AI merge-conflict resolver (rizzler-style driver) — upgrades the `merge.yaml` agent rule 12 dispatches. Until #12728, rule 11 escalates and rule 12 runs the current merge stub (clean merges work; conflicts escalate from within the agent).
- **BMAD porting** (future): Port `bmad-create-prd`, `bmad-validate-prd`, `bmad-product-brief`, `bmad-advanced-elicitation`, `bmad-testarch-*` into Gobby skills for the interactive side of the product. No rule ever dispatches those skills from the autonomous loop.
- **`requirements-analyst` dispatch rule**: not in scope; interactive-only usage.
- **GitHub-issue → autonomous-loop → PR demo** (the killer feature): future epic. Requires #12728 plus a new "issue-importer" rule/agent that creates tasks from GitHub issues on watched repos.
- **Autonomous self-improvement loop** (yolo-driven, no human in loop): future epic. Infrastructure exists once yolo flag lands; agent design is its own large effort.
- **Gobby → Rust port of the dispatcher module**: explicitly deferred. Current decision logic is cold-path; Python is the right tool. Revisit when a real hot path emerges.
- **Finer-grained tech skills** (`react`, `django`, `sqlalchemy`, `playwright`, etc.): authoring those as standalone gobby skills is follow-up work. This epic ships only the two role-level agent templates (`frontend-developer`, `backend-developer`) with their own baseline-skills blocks, plus the expander's wiring to populate `additional_skills` when needed. Agents degrade gracefully when `additional_skills` lists a skill that doesn't exist (log + continue).
- **PostgreSQL migration + cloud backup**: deferred as its own epic. The schema designed here (six columns on `tasks` + three 1:1-adjacent tables) is PG-ready: same column types, same constraints, same indexes. The migration from SQLite to PG is straightforward data copy + `tasks.jsonl` retirement. This plan does not depend on tasks.jsonl git-sync for any of its audit trails; all audit data (lifecycle events, dispatcher ticks, description markers) is database-native or filesystem-local.
- **Additional built-in agents** (`docs-writer`, `migration-runner`, `security-reviewer`, etc.): follow-up work driven by audits of `## Agent Selection` markers — when `backend-developer` default shows up repeatedly for a specific kind of task, that's the signal to author a specialized agent. Expander prompt tuning follows the same audit feedback loop.
- **`auto` profile with LLM-assisted classification**: current plan ships a rule-based `auto` (plan-file → review; leaf → quick; epic with plan → full; epic without plan → error). LLM-based classification is a follow-up if rule-based proves too coarse.
- **Daemon-side `mark_task_review_rejected` formatting fix**: the rejection-append code in `src/gobby/storage/tasks/_transitions.py` emits a duplicate `## Adversary Findings — Round N` heading when `rejection_notes` already starts with that heading. Cosmetic only; filed as its own task. Not part of this lifecycle epic.
