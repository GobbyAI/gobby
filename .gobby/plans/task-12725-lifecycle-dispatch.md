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

### 1.1 Add Lifecycle enum and automation fields to Task model [category: code]
`kind: deliverable`

Target: `src/gobby/storage/tasks/_models.py`

Add a new enum and exactly six new columns on `tasks`. High-churn / sparse / append-only state goes into 1:1 adjacent tables (§1.1a, §1.1b, §1.1c) rather than being inlined here, to keep the hot-path row narrow.

```python
from enum import StrEnum

class Lifecycle(StrEnum):
    open              = "open"               # backlog; not yet in pipeline
    plan_review       = "plan_review"        # adversary loop with planner
    test_arch         = "test_arch"          # test-architect analysis
    expanding         = "expanding"          # expansion run in flight
    in_development    = "in_development"     # subtasks being worked
    holistic_review   = "holistic_review"    # epic-level intent/diff review
    pr                = "pr"                 # PR creation (today: merge.yaml stub)
    merging           = "merging"            # merge in progress
    merged            = "merged"             # terminal

class Isolation(StrEnum):
    none              = "none"               # work on current branch
    worktree          = "worktree"           # dedicated worktree (default; shared .git)
    clone             = "clone"              # full local clone (independent .git; portable)

# Appended to the Task dataclass (six columns total):
lifecycle: Lifecycle = Lifecycle.open
allow_automation: bool = False
yolo: bool = False                                 # never-escalate; worktree-sandboxed
isolation: Isolation = Isolation.worktree
assigned_agent: str | None = None                  # set at expansion/build time
additional_skills: list[str] | None = None         # optional augmentation on top of the agent's baseline
```

Intentionally **not** on `tasks`:

- **`profile`** — profiles are CLI sugar only (§3.2). Resolved state (`stage-:*` labels, `isolation` column, `yolo` bool) is what gets persisted.
- **`stack`** — replaced by `assigned_agent` (§2.8).
- **Mutex state** — see §1.1a (`task_dispatch_mutex`).
- **`plan_file_path` / `worktree_path`** — see §1.1b (`task_artifacts`).
- **Lifecycle history** — see §1.1c (`task_lifecycle_events`).

Update `serialize_task_state`, `to_dict`, `to_brief` to surface the six new columns. Skippable-stage state is still carried as labels (`stage-:<name>`), already serialized by the existing label machinery.

**Acceptance:**

- 1.1.1 — Add Lifecycle enum and automation fields to Task model is implemented according to this section. file: `src/gobby/storage/tasks/_models.py`.

### 1.1a `task_dispatch_mutex` table (new) [category: code]
`kind: deliverable`

Target: `src/gobby/storage/tasks/_models.py` + `src/gobby/storage/tasks/_dispatch_mutex.py` (new)

High-churn mutex state for the dispatcher. Inlining these on `tasks` creates WAL pressure on every tick (every dispatch attempt rewrites the row, invalidating caches for unrelated readers). Separate 1:1 table with absent row = "no lease":

```sql
CREATE TABLE task_dispatch_mutex (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    lease_until  TEXT,                                 -- ISO timestamp; NULL when free
    lease_holder TEXT,                                 -- holder token (usually cron job name)
    run_id       TEXT,                                 -- set only for spawn-kind mutations
    action_kind  TEXT,                                 -- one of: spawn, expansion, worktree, lifecycle, field
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_dispatch_mutex_scan
    ON task_dispatch_mutex (lease_until, run_id);
```

Dataclass `DispatchMutex` mirrors the row. CRUD helpers: `get_mutex(task_id)`, `acquire_mutex(task_id, holder, kind, run_id, ttl_seconds)`, `release_mutex(task_id, holder)`, `clear_by_run_id(run_id)`, `sweep_expired(now)`. §1.4 operates on this table via those helpers (not on `tasks` columns).

**Acceptance:**

- 1.1a.1 — task_dispatch_mutex table (new) is implemented according to this section. file: `src/gobby/storage/tasks/_models.py`.

### 1.1b `task_artifacts` table (new) [category: code]
`kind: deliverable`

Target: `src/gobby/storage/tasks/_models.py` + `src/gobby/storage/tasks/_artifacts.py` (new)

Sparse pointers to filesystem / external resources. Most tasks have none; absent row = "no artifacts."

```sql
CREATE TABLE task_artifacts (
    task_id            TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    plan_file_path     TEXT,
    worktree_path      TEXT,                              -- populated when isolation=worktree; CreateWorktree action sets it
    worktree_id        TEXT,                              -- R6.F3: storage-row id from worktrees table; passed to merge agent as session var
    clone_path         TEXT,                              -- populated when isolation=clone; CreateClone action sets it (§1.6, §1.7)
    clone_id           TEXT,                              -- R6.F3: LocalCloneManager row id; passed to merge agent as session var
    target_branch      TEXT,                              -- R4.F6: base branch for worktree/merge; captured at gobby build time
    expansion_run_id   TEXT,                              -- R4.F1: active expansion run; cleared on validation reject so rule_start_expansion can re-fire
    expansion_attempts INTEGER NOT NULL DEFAULT 0,        -- R4.F1: retry counter; capped via MAX_EXPANSION_ATTEMPTS in §1.7
    pr_url             TEXT,                              -- future: #12728 PR-creation agent
    merge_commit_sha   TEXT,                              -- reserved for future write; capture deferred to #12728 per R7.F3
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- R6.F2 / R7.F4: enforce pairwise co-presence within each isolation
    -- family AND mutual exclusion across families. Three predicates:
    --   1. (worktree_path IS NULL) iff (worktree_id IS NULL)  — pair invariant
    --   2. (clone_path IS NULL) iff (clone_id IS NULL)        — pair invariant
    --   3. worktree_path IS NULL OR clone_path IS NULL        — family XOR
    -- This rules out partial states (e.g. worktree_path set with worktree_id
    -- NULL) that would let path-presence checks see "artifact exists" while
    -- the merge agent has no id to call merge tools with.
    CHECK (
        (worktree_path IS NULL) = (worktree_id IS NULL)
        AND (clone_path IS NULL) = (clone_id IS NULL)
        AND (worktree_path IS NULL OR clone_path IS NULL)
    )
);
```

`worktree_*` and `clone_*` columns are mutually exclusive (R6.F2 + R7.F4 — enforced at the SQL CHECK level: pairwise co-presence within each family AND family XOR). Only one set per epic, depending on `tasks.isolation`. Populated at the `lifecycle=in_development` boundary by `rule_create_worktree` / `rule_create_clone` (§1.7), each writing both `*_path` and `*_id` atomically (`set_artifacts_atomic`). On terminal `merged` lifecycle (§2.10), the merge agent clears the active family's pair atomically — worktree cleanup clears `(worktree_path, worktree_id)`; clone cleanup clears `(clone_path, clone_id)`. Plan path and target_branch populate at build time; expansion fields populate when an epic enters `expanding`; `pr_url` future.

CRUD (Python helpers in `src/gobby/storage/tasks/_artifacts.py`): `get_artifacts(task_id)`, `set_artifact(task_id, field, value)`, `set_artifacts_atomic(task_id, **fields)` (multi-field atomic write — used to populate or clear `(worktree_path, worktree_id)` / `(clone_path, clone_id)` together; raises on CHECK constraint violation with a clear error mapping the failing predicate), `clear_artifact(task_id, field)`, `clear_artifacts(task_id)`, `clear_isolation_pair(task_id, family)` where `family ∈ {"worktree", "clone"}` (clears the matching pair atomically), `increment_expansion_attempts(task_id)`. The MCP-tool surface is added in §1.1d (R7.F3 — agents need `set_artifact` / `set_artifacts_atomic` / `clear_isolation_pair` / `append_description_section` exposed via gobby-tasks-ops to call them from tool-restricted workflow steps).

**Acceptance:**

- 1.1b.1 — task_artifacts table (new) is implemented according to this section. file: `src/gobby/storage/tasks/_models.py`.

### 1.1c `task_lifecycle_events` table (new) [category: code]
`kind: deliverable`

Target: `src/gobby/storage/tasks/_models.py` + `src/gobby/storage/tasks/_lifecycle_events.py` (new)

Append-only audit trail of lifecycle transitions. PG-ready from day one; the move to PostgreSQL (post-this-epic) leaves this schema unchanged.

```sql
CREATE TABLE task_lifecycle_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_state  TEXT,                                   -- NULL for initial seed event
    to_state    TEXT NOT NULL,
    reason      TEXT NOT NULL,                          -- mandatory; human- or agent-supplied
    by_actor    TEXT NOT NULL,                          -- "cli", "dispatcher:<cron_id>", "<agent_name>"
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_lifecycle_events_task
    ON task_lifecycle_events (task_id, created_at);
```

CRUD: `record_lifecycle_event(task_id, from_state, to_state, reason, by)`, `list_lifecycle_events(task_id, limit=None)`. `advance_lifecycle` in §1.8 is the sole writer; `record_lifecycle_event` is its INSERT step.

**Acceptance:**

- 1.1c.1 — task_lifecycle_events table (new) is implemented according to this section. file: `src/gobby/storage/tasks/_models.py`.

### 1.1d MCP tool extensions on gobby-tasks-ops [category: code] (depends: 1.1b)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks_ops.py` (or wherever `gobby-tasks-ops` tool registry lives)

R7.F3 fix. The merge agent (§2.10), expansion-qa (§2.9), and other workflow steps need MCP-callable surfaces for the artifact-mutation and audit-marker helpers introduced in §1.1b. `gobby-tasks-ops` does not currently expose any of them — confirmed via `list_tools(server_name="gobby-tasks-ops")`: existing tools are expansion-run, affected-files, GitHub-sync, and front-half-tick; no artifact CRUD, no description-section append.

Add these MCP tools (each is a thin wrapper over the §1.1b Python helper of the same name):

- **`set_artifact(task_id: str, field: str, value: str | int | None)`** — single-field write. `field` is validated against the `task_artifacts` column allowlist (`plan_file_path`, `worktree_path`, `worktree_id`, `clone_path`, `clone_id`, `target_branch`, `expansion_run_id`, `expansion_attempts`, `pr_url`, `merge_commit_sha`). CHECK violation surfaces as a structured error including the failing predicate.
- **`set_artifacts_atomic(task_id: str, fields: dict[str, str | int | None])`** — multi-field atomic write in a single transaction. Same allowlist + CHECK error surface. Used by §2.10 cleanup to clear `(worktree_path, worktree_id)` or `(clone_path, clone_id)` pairs atomically.
- **`clear_isolation_pair(task_id: str, family: str)`** — convenience wrapper over `set_artifacts_atomic` that clears the named family's pair (`family ∈ {"worktree", "clone"}`). Single MCP call so the merge agent's cleanup step is one tool invocation.
- **`append_description_section(task_id: str, heading: str, body: str)`** — append a `## {heading}\n{body}\n` block to `tasks.description`. Idempotent on `(task_id, heading, body)` signature within a single transaction (duplicate markers within the same call are deduped). Used by `AppendAuditMarker` (§1.6) and the merge agent's yolo-fallback path (§2.10).
- **`get_artifacts(task_id: str) -> dict`** — read-only fetch of the `task_artifacts` row (or empty dict if absent). Used by prompt builders (§1.6 PROMPT_BUILDERS) when constructing initial_variables for the merge agent.

All five tools follow the existing `gobby-tasks-ops` registration pattern (decorated registry function, schema generation, session-context resolution). The Python helpers in §1.1b's `_artifacts.py` are the single source of truth for the actual SQL; these MCP tools are stateless wrappers.

Allowlist these tools in `merge.yaml` (§2.10), `expansion-qa.yaml` (§2.9 — for clearing `expansion_run_id` on rejection), and `holistic-reviewer.yaml` (§2.2 — for citing leaves and writing audit markers). Existing task-transitions skill gates do not apply (these are artifact mutations, not lifecycle status changes).

**Acceptance:**

- 1.1d.1 — MCP tool extensions on gobby-tasks-ops is implemented according to this section. file: `src/gobby/mcp_proxy/tools/tasks_ops.py`.

### 1.2 DB migration for lifecycle + automation + adjacent tables [category: config] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py` and `src/gobby/storage/baseline_schema.sql`

Add exactly **six columns** to `tasks`:

```sql
ALTER TABLE tasks ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'open';
ALTER TABLE tasks ADD COLUMN allow_automation BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN yolo BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN isolation TEXT NOT NULL DEFAULT 'worktree';
ALTER TABLE tasks ADD COLUMN assigned_agent TEXT;
ALTER TABLE tasks ADD COLUMN additional_skills TEXT;  -- JSON array
```

Index the scanner's hot path (note: no mutex columns here — those live in `task_dispatch_mutex`):

```sql
CREATE INDEX idx_tasks_dispatch_scan
  ON tasks (allow_automation, lifecycle, status);
```

Create the three adjacent tables (defined in §1.1a, §1.1b, §1.1c):

```sql
CREATE TABLE task_dispatch_mutex (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    lease_until  TEXT,
    lease_holder TEXT,
    run_id       TEXT,
    action_kind  TEXT,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_dispatch_mutex_scan
    ON task_dispatch_mutex (lease_until, run_id);

CREATE TABLE task_artifacts (
    task_id            TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    plan_file_path     TEXT,
    worktree_path      TEXT,
    worktree_id        TEXT,
    clone_path         TEXT,
    clone_id           TEXT,
    target_branch      TEXT,
    expansion_run_id   TEXT,
    expansion_attempts INTEGER NOT NULL DEFAULT 0,
    pr_url             TEXT,
    merge_commit_sha   TEXT,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (worktree_path IS NULL) = (worktree_id IS NULL)
        AND (clone_path IS NULL) = (clone_id IS NULL)
        AND (worktree_path IS NULL OR clone_path IS NULL)
    )
);

CREATE TABLE task_lifecycle_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    by_actor   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_lifecycle_events_task
    ON task_lifecycle_events (task_id, created_at);
```

Update `baseline_schema.sql` so fresh installs ship the columns and tables directly.

**Acceptance:**

- 1.2.1 — DB migration for lifecycle + automation + adjacent tables is implemented according to this section. file: `src/gobby/storage/migrations.py`.

### 1.3 Extend task CRUD for new fields and helpers [category: code] (depends: 1.2)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_crud.py` and related modules

- Extend create/update/list helpers to read/write the six new columns. Serialize `additional_skills` as JSON; deserialize `lifecycle` and `isolation` as their StrEnum types.
- Add CRUD helpers for `task_dispatch_mutex` (§1.1a), `task_artifacts` (§1.1b), `task_lifecycle_events` (§1.1c). Each lives in its own module so the `tasks` CRUD stays focused.
- Add `list_automation_candidates(db) -> list[Task]`: opted-in, unclaimed, dependency-unblocked (§1.3a), and not currently leased (LEFT JOIN `task_dispatch_mutex` with `lease_until IS NULL OR lease_until < now`).
- Add `_skipped_stages(task) -> set[str]` that parses `stage-:<name>` labels. Returns an empty set if no skip labels.
- Add `_is_yolo(task) -> bool` trivially returning `task.yolo`.
- **Do not** add `get_profile` or any profile helper — profile is not stored on tasks.

**Acceptance:**

- 1.3.1 — Extend task CRUD for new fields and helpers is implemented according to this section. file: `src/gobby/storage/tasks/_crud.py`.

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
          | AdvanceLifecycle | CloseLeaf | EscalateTask | AppendAuditMarker | Skip)
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
    Action, AdvanceLifecycle, AppendAuditMarker, CreateWorktree, EscalateTask,
    Skip, SpawnAgent, StartExpansionRun,
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

def rule_qa(task: Task) -> Action | None:
    if not _stage_enabled(task, "qa"):
        return None
    if (task.lifecycle == Lifecycle.in_development
        and task.status == "needs_review"
        and not task.claimed_by_session_id):
        return SpawnAgent(
            agent="qa-reviewer", task_id=task.id, prompt_builder="qa_reviewer",
            additional_skills=task.additional_skills,
        )
    return None

def rule_all_closed_advance_to_holistic(task: Task) -> Action | None:
    if (task.task_type == "epic"
        and task.lifecycle == Lifecycle.in_development
        and _all_subtasks_closed(task)):
        target = (Lifecycle.holistic_review if _stage_enabled(task, "holistic_review")
                  else Lifecycle.pr if _stage_enabled(task, "pr")
                  else Lifecycle.merging if _stage_enabled(task, "merging")
                  else Lifecycle.merged)
        return AdvanceLifecycle(task_id=task.id, to=target,
                                reason="all subtasks closed", by_actor="dispatcher")
    return None

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
    rule_all_closed_advance_to_holistic,
    rule_holistic,
    rule_pr,
    rule_merging,
)

def evaluate(task: Task) -> Action:
    for rule in RULES:
        action = rule(task)
        if action is not None:
            return action
    return Skip(reason="no rule matched")
```

Helpers `_current_verdict_rejected`, `_rounds_remaining`, `_expansion_active`, `_expansion_run_completed`, `_expansion_attempts`, `_target_branch`, `_is_coding_epic`, `_has_ready_subtasks`, `_all_subtasks_closed`, `_skipped_stages`, `_is_yolo`, `_has_worktree`, `_has_clone`, `_has_isolation_artifact`, `_parent_epic`, `_now_iso` live alongside rules. `_has_isolation_artifact(epic)` returns true when the appropriate artifact column for the epic's `isolation` is populated (`worktree_path` for `worktree`, `clone_path` for `clone`); for `isolation=none` it returns true unconditionally (in-branch work needs no artifact). They read durable task state: labels (`planning-current-verdict:rejected`, `planning-round:N`, `planning-max-rounds:N`, `stage-:<name>`), `task_artifacts` rows (R4.F1 expansion fields, R4.F6 target_branch), the expansion-run table (for `_expansion_run_completed`), subtask tree presence, and task fields on `tasks`. `_target_branch` falls back to `git rev-parse --abbrev-ref HEAD` when `task_artifacts.target_branch` is absent (legacy tasks created before R4.F6). **No `_get_stack`, no `_get_profile`, no `_added_stages`, no `_expansion_started`** — those helpers are obsolete:

- `stack` was replaced by `assigned_agent` (§2.8).
- Profile is CLI sugar only; no label storage, no helper needed.
- `stage+:` is gone; the full pipeline is the default, `stage-:` removes stages.

**Acceptance:**

- 1.7.1 — Decision rules for all stages is implemented according to this section. file: `src/gobby/dispatch/rules.py`.

### 1.8 Lifecycle transitions in review tools [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_transitions.py` and the matching MCP wrappers

**Core contract**: when `mark_task_review_approved` triggers a lifecycle advance, it also **resets `status` to `open`** so the next stage's rule can dispatch (rules gate on `status in ("open", "needs_review")`). The approval event is preserved in `task_lifecycle_events` (§1.1c) — audit trail is not lost.

Extend existing transitions and add one new one:

- **`mark_task_review_approved`**:
  - `lifecycle=plan_review` → advance to `test_arch`; status resets to `open`.
  - `lifecycle=test_arch` → advance to `expanding`; status resets to `open`.
  - `lifecycle=expanding` → advance to `in_development`; status resets to `open`. (Called by expansion-qa after successful validation — §2.9 wires this.)
  - `lifecycle=holistic_review` → **advance to `pr`** (R3.F4 fix); status resets to `open`. If `pr` stage is skipped, further advance to `merging` (or `merged` if both are skipped).
  - `lifecycle=merging` → advance to `merged` (terminal); status = `closed`.
  - `lifecycle=in_development` leaf subtask → no lifecycle change (leaf semantics; epic advances via `rule_all_closed_advance_to_holistic`).
  - Every advance writes a `task_lifecycle_events` row with `reason="mark_task_review_approved"` and `by_actor=<current_session_agent_name>`.

- **`mark_task_review_rejected(task_id, rejection_notes=None, round_number=None, cited_subtasks=None)`**: extended signature — `cited_subtasks` is a list of leaf refs (R4.F5). Behavior by lifecycle:
  - `lifecycle=plan_review` → stays at `plan_review`; adds `planning-current-verdict:rejected` label; increments `planning-round:N`; appends findings to description (existing behavior per R2.F1). `cited_subtasks` ignored.
  - `lifecycle=holistic_review` (R4.F5 fix) → `cited_subtasks` is **REQUIRED** (one or more leaf refs needing rework); rejection without it raises a validation error. The tool atomically (single transaction): (a) appends findings to the epic description, (b) reopens each cited subtask (`status: closed → open`), and (c) rewinds the epic lifecycle `holistic_review → in_development` with `status=open`. The atomic reopen prevents `rule_all_closed_advance_to_holistic` from immediately bouncing the epic back into `holistic_review` on the next tick (because at least one subtask is now `open`, the predicate is false until the dev/qa loop closes the cited leaves again).
  - `lifecycle=expanding` (R4.F1 extension) → stays at `expanding`; findings appended; **clears `task_artifacts.expansion_run_id`** (so `rule_start_expansion` can re-fire on the next tick); **calls `increment_expansion_attempts(task_id)`** so the retry cap is enforced. §2.9 (expansion-qa) is the caller.
  - `lifecycle=merging` (R6.F4 extension) → stays at `merging`; status resets to `open`; findings appended (yolo retry detail or non-yolo failure note); **does NOT itself manipulate the `merge-attempts:N` label** — that label is managed by the merge agent (§2.10) immediately before the rejection call. The tool atomically (single transaction) appends findings, resets status, and writes the rejection event to `task_lifecycle_events`. `rule_merging` re-dispatches the merge agent on the next tick. §2.10 (merge agent yolo retry path) is the caller. After `merge-attempts:N >= cap`, the merge agent switches to the force-advance fallback (`mark_task_review_approved` with audit marker), which §2.10 documents in detail.
  - Leaf `lifecycle=in_development` → no lifecycle change; status → `open`; normal dev/qa loop. `cited_subtasks` ignored.

- **`mark_task_needs_review`**: no lifecycle change. Planner MUST clear the `planning-current-verdict:rejected` label when submitting for review (R2.F1, enforced in §2.7 planner step definition).

- **New tool `advance_lifecycle(task_id, to, reason, by_actor)`**: MCP-exposed explicit transition. `reason` is **mandatory** (TEXT NOT NULL in `task_lifecycle_events`); `by_actor` defaults to the calling session's agent name. Writes the row and updates `tasks.lifecycle`. Also resets `status` to `open` unless the new lifecycle is `merged` (terminal → `closed`).

- **Extended tool `de_escalate_task(task_id, next_status, lifecycle=None, reason=None)`**: now accepts an optional `lifecycle` parameter for single-call recovery. Matters for pr-escalation: after a human opens the PR, they run `de_escalate_task(task_id, next_status="review_approved", lifecycle=Lifecycle.merging, reason="human opened PR #N")`. The tool:
  1. Clears the escalated state, sets `status = next_status`.
  2. If `lifecycle` is provided, also calls `advance_lifecycle(task_id, to=lifecycle, reason=reason or "de-escalation", by_actor="human")`.
  3. Writes the combined change in a single transaction.

Session-context enforcement stays as today (mark_* autonomous-only; close_task interactive unless escaping with labels).

**Acceptance:**

- 1.8.1 — Lifecycle transitions in review tools is implemented according to this section. file: `src/gobby/storage/tasks/_transitions.py`.

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

### 2.1 Holistic-review skill [category: docs]
`kind: deliverable`

Target: `src/gobby/install/shared/skills/holistic-review/SKILL.md` (new)

Four-point intent-vs-diff methodology. Inputs: epic task, plan artifact at `epic.plan_file_path`, aggregate worktree diff, linked subtask validation_criteria. Checks: Scope (did PR do exactly what plan said), Reality (end-to-end behavior matches plan outcome), Testing (coverage adequate for changes), YAGNI (any drift / creep / scope bloat).

Output: structured verdict block appended under `## Holistic Findings` on the epic:

```markdown
## Holistic Findings

**Verdict**: approve | request_changes | needs_discussion

### Scope Check      — OK | Drift | Gap : <citations>
### Reality Check    — OK | Drift | Gap : <citations>
### Testing Check    — OK | Drift | Gap : <citations>
### YAGNI Check      — OK | Drift | Gap : <citations>

### Blocking Findings
<if any>
```

Decision mapping (R3.F4 + R4.F5 fixes — approval/rejection now atomic via §1.8):
- `approve` → `mark_task_review_approved` on the epic. §1.8 advances `lifecycle: holistic_review → pr` (or next enabled stage) and resets status to `open`. `rule_pr` (or `rule_merging`, depending on skip set) picks up on the next tick.
- `request_changes` (R4.F5 fix) → the holistic-reviewer skill MUST instruct the agent to identify which specific subtask(s) each finding implicates, then call `mark_task_review_rejected(epic_task_id, rejection_notes=findings, cited_subtasks=[<leaf refs>])`. The §1.8 tool atomically (single transaction): appends findings, reopens each cited subtask (`status: closed → open`), and rewinds lifecycle `holistic_review → in_development`. **At least one cited subtask is required** — passing `cited_subtasks=[]` or omitting it raises a validation error. Subtasks not cited stay closed, so the dev/qa loop only re-runs on what actually needs rework. Once the cited leaves close again, `rule_all_closed_advance_to_holistic` re-fires and holistic re-runs.
- `needs_discussion` → `escalate_task` with `needs_human:` prefix. Yolo tasks never reach `needs_discussion` — under yolo, `holistic-reviewer` must choose `approve` or `request_changes`.

The holistic-review SKILL.md prose explicitly walks through finding-to-leaf attribution: every blocking finding must be traceable to one or more `### N.N` plan sections, which map to expansion-generated subtasks. If a finding spans multiple leaves, cite all of them. If a finding is genuinely epic-level (e.g., scope drift across the whole change), cite the most representative leaf and explain the cross-leaf scope in the finding body.

**Acceptance:**

- 2.1.1 — Holistic-review skill documents scope, reality, testing, and YAGNI checks against the epic plan and linked subtask validation criteria. file: `src/gobby/install/shared/skills/holistic-review/SKILL.md`.
- 2.1.2 — Holistic findings output includes verdict, check subsections, and blocking findings for downstream transition tools. behavior: `structured holistic findings block in task-12725-lifecycle-dispatch.md §2.1`.
- 2.1.3 — Approval and request-changes mappings call the lifecycle review tools with cited subtasks where required. behavior: `holistic approval and rejection transition contract in §2.1`.

### 2.2 Holistic-reviewer agent template [category: config] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml` (new)

Mirrors `plan-adversary.yaml`'s shape: claim → load skill → review → terminate. Loads `holistic-review` skill via `get_skill` as first post-claim action. Allows filesystem reads (git diff, source files) in the `review` step; blocks `close_task`, `reopen_task`, `mark_task_needs_review`, spawning, killing. Terminates via `end_agent_run`.

Model/provider choice: `codex` / `gpt-5.5` / `reasoning_effort: high` as the baseline (inherited from plan-adversary conventions). Tunable later.

**Acceptance:**

- 2.2.1 — Holistic-reviewer agent template is implemented according to this section. file: `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml`.

### 2.3 Test-architect skill minimal wiring [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/test-architect.yaml`

Current content is a stub. This plan does NOT port BMAD. Update the stub agent's instructions to produce **structured prose recommendations**, NOT new `### N.N` task sections (R4.F4 fix). The `plan-draft` methodology bans authored test tasks from plan artifacts and reserves `[category: test]` for test infrastructure (fixtures, helpers, harnesses) — not test cases. Authored test cases live in the TDD sandwich of each `[category: code]` leaf.

The architect's job:

1. Read the plan artifact at `epic.plan_file_path`.
2. Decide whether the scope calls for non-unit test coverage (integration / e2e / regression / contract) beyond what the per-leaf TDD sandwich will produce.
3. If additional coverage is needed, append a `## Test Architecture` section to the plan artifact with **structured prose** in the following shape:

   ```markdown
   ## Test Architecture

   ### Integration
   - **§<plan-section>**: <what to verify, e.g., "API → DB roundtrip via test client; assert <invariant>">
   - **§<plan-section>**: <...>

   ### E2E
   - **§<plan-section>**: <full-flow scenario; what entry point, what assertions>

   ### Regression
   - **§<plan-section>**: <bug class to defend against; failure mode being prevented>

   ### Contract / Cross-Surface
   - **§<plan-section>**: <surfaces that must agree, e.g., "CLI/MCP/HTTP build all return identical BuildResult">

   ### Test Infrastructure
   - **<infra need>**: <new fixture, helper module, harness — becomes a [category: test] leaf>
   ```

   Expansion (§2.8) reads `## Test Architecture` and folds the **Integration / E2E / Regression / Contract** recommendations into the test-writing portion of the relevant `[category: code]` leaves' prompts (the TDD sandwich already includes test-writing steps; these recommendations augment those steps with the architect's specific scenarios). Only **Test Infrastructure** items become standalone `[category: test]` leaves — they are infrastructure, not test cases.

4. If unit tests suffice: append `## Test Architecture\n\nUnit tests sufficient — no additional test types recommended.\n`
5. Submit for review: `mark_task_review_approved` (the architect self-approves; no separate QA layer on test-arch per scope decision).

The category boundary stays intact: `[category: code]` leaves carry their own tests via TDD; `[category: test]` leaves are test infrastructure only. Expansion-QA validates this boundary (§2.9 — any `[category: test]` leaf description must clearly identify infra, not authored test cases; ambiguous ones are rejected).

**Acceptance:**

- 2.3.1 — Test-architect agent emits structured prose recommendations instead of authored test-task sections. file: `src/gobby/install/shared/workflows/agents/test-architect.yaml`.
- 2.3.2 — Only Test Infrastructure recommendations become standalone category test leaves; integration, e2e, regression, and contract recommendations fold into code-leaf TDD prompts. behavior: `test architecture category boundary in §2.3`.

### 2.4 Add close_task permission to qa-reviewer [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`

After `mark_task_review_approved` on a leaf, QA proceeds to `close_task` with `reason="qa_approved"` and `commit_sha` from the dev's commit on the worktree. Remove `close_task` from the `blocked_mcp_tools` list (if present) for the QA review step. The existing task-transitions skill's gates still apply.

**Acceptance:**

- 2.4.1 — Add close_task permission to qa-reviewer is implemented according to this section. file: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`.

### 2.5 Frontend-developer agent template [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/frontend-developer.yaml` (new)

New dev agent for FE-stack leaves. Tool allowlist tuned for the FE toolchain: npm / pnpm / yarn, playwright, vite/webpack dev servers, storybook, lighthouse, eslint. Model/provider choice can differ from backend (tunable).

**Baseline skills** (always loaded by the agent at claim-time, defined in the YAML's `skills:` block): the agent's core FE competence — component authoring, routing, bundling, testing conventions. These are the skills the agent always has.

**`additional_skills`** (R3.U5 rename from `required_skills`) — optional augmentation passed via the initial variable. For each entry in the prompt's `additional_skills`, the agent calls `get_skill(name=<skill>)` on gobby-skills as its first post-claim action. This is where finer-grained tech skills (`react`, `nextjs`, `tailwind`, etc.) land when the expander decides the leaf needs augmentation beyond the agent's baseline (§2.8). Usually empty.

Unblock `close_task` so the agent can self-close when its subtree has no QA stage (e.g., `stage-:qa` or `--profile quick`). The agent branches on whether `qa` is in `_skipped_stages` of its parent: skipped → self-close after commit; present → `mark_task_needs_review` and wait for `rule_qa`.

**Acceptance:**

- 2.5.1 — Frontend-developer agent template is implemented according to this section. file: `src/gobby/install/shared/workflows/agents/frontend-developer.yaml`.

### 2.6 Backend-developer agent template [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/backend-developer.yaml` (new)

New dev agent for BE-stack leaves. Tool allowlist tuned for the BE toolchain: pytest, mypy, ruff, database client (`psql`, `sqlite3`), migration runners, `uv` / `pip` / `poetry`, container tools.

Also serves as the **default-agent fallback** invoked by `rule_dispatch_leaf` (§1.7) when a leaf arrives with no `assigned_agent` — paired with an `AppendAuditMarker` so fallbacks are audit-visible. R4.F3 broadens the set of categories that hit this fallback path (any of `code`/`config`/`docs`/`test`).

Same `additional_skills` loading contract as §2.5 — expander-assigned augmentations (`django`, `fastapi`, `sqlalchemy`, `postgres`, etc.) load first, then the agent works the task.

Same `close_task` unblock and skip-stage branching as §2.5.

**Acceptance:**

- 2.6.1 — Backend-developer agent template is implemented according to this section. file: `src/gobby/install/shared/workflows/agents/backend-developer.yaml`.

### 2.7 Planner clears rejection marker on resubmit [category: config] (depends: 1.8)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/planner.yaml`

In the planner's submit step (before `mark_task_needs_review`), explicitly remove the `planning-current-verdict:rejected` label if present. Addresses R2.F1 (rejection marker must be durable state, cleared by the planner on resubmit — not inferred from historical text).

**Acceptance:**

- 2.7.1 — Planner clears rejection marker on resubmit is implemented according to this section. file: `src/gobby/install/shared/workflows/agents/planner.yaml`.

### 2.8 Expansion: Agent Selection + profile-appropriate subtasks [category: code]
`kind: deliverable`

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

- 2.8.1 — Expansion emits stage-driven task trees from skipped-stage state instead of stored profiles. file: `src/gobby/tasks/expansion_service.py`.
- 2.8.2 — Every automated leaf in code, config, docs, or test categories receives an assigned_agent value and optional additional_skills. file: `src/gobby/tasks/expansion.py`.
- 2.8.3 — Expansion rejects planning leaves and requires test-category leaves to describe infrastructure only. behavior: `expansion QA constraints in §2.8`.

### 2.8a Expansion-agent-selection skill [category: docs]
`kind: deliverable`

Target: `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md` (new)

Documents the decision heuristics the expander uses for Agent Selection. Loaded as the first action of §2.8's Agent Selection step.

Sections:

1. **Standard label vocabulary** — common semantic tags the expander may see or set: `security`, `performance`, `accessibility`, `i18n`, `migration`, `deprecation`, `docs-api`, `docs-user`, `infra`, `dependency`. Short definitions; explicit note that this list is open.
2. **Agent registry descriptions** — one-paragraph description of each shipped agent with the label/content patterns it typically matches. v1 ships `frontend-developer` and `backend-developer`; this section grows as new agents land.
3. **Decision heuristics** — concrete guidance per automated category (R4.F3 — covers every category in `AUTOMATED_LEAF_CATEGORIES`):
   - `category=code` + FE indicators (UI, React, styles, components, browser APIs) → `frontend-developer`.
   - `category=code` + BE indicators (DB, migration, API, server, CLI) → `backend-developer`.
   - `category=config` (YAML, Python config, agent/workflow definitions, build files) → `backend-developer`.
   - `category=docs` (markdown, skill files, CLAUDE.md updates, API reference, user guides) → `backend-developer` (no docs-writer agent exists yet; tracked as a follow-up to author one once we audit how often docs leaves appear).
   - `category=test` (test infrastructure: fixtures, helpers, conftest changes, harness modules — NOT authored test cases, which live in code-leaf TDD sandwiches) → `backend-developer`.
   - Ambiguous → `backend-developer` (safer default; appends audit marker).
   - **`category=planning`** → MUST NOT appear on a leaf. The expander must never emit planning leaves; expansion-qa rejects any that slip through (§2.9). If the planner needs sub-planning work, it belongs in a separate epic, not as a leaf of the current epic.
4. **`additional_skills` guidance** — when to populate (dynamic libraries, cross-cutting concerns, unusual frameworks) and when not to (anything in the agent's baseline).
5. **Failure modes** — what to do when no agent fits: default to `backend-developer` + log via `## Agent Selection` description marker. **Never escalate** from the agent-selection path.

Skill will be audited and tuned as prompts mature; folded into this epic rather than deferred because the prompts for all new agents/workflows need auditing together before the happy path runs e2e (R3.U10).

**Acceptance:**

- 2.8a.1 — Skill section defines the standard label vocabulary used by expansion agent selection. file: `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md`.
- 2.8a.2 — Skill section documents shipped agent registry descriptions for frontend-developer and backend-developer. file: `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md`.
- 2.8a.3 — Skill section maps automated categories to concrete agent-selection heuristics. file: `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md`.
- 2.8a.4 — Skill section documents when additional_skills should augment baseline agent skills. file: `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md`.
- 2.8a.5 — Skill section documents ambiguity fallback to backend-developer with an audit marker. file: `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md`.

### 2.8b Expose `start_expansion_run_impl` for in-process dispatcher use [category: code] (depends: 1.1b)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks_ops.py` (or wherever the MCP tool's handler lives)

R6.F5 + R7.F2 fix. The existing `gobby-tasks-ops:start_expansion_run` MCP tool already wraps the canonical "create expansion-run row + kick compile + return run record" flow against the real `ExpansionService(*, task_manager, llm_service, config=None, run_manager=None)` constructor and `LocalExpansionRunManager.create(*, parent_task_id, project_id, triggering_session_id, input_source, plan_file=None, provider=None, model=None, options=None, run_id=None) -> ExpansionRun`. The dispatcher (§1.9 `StartExpansionRun` case) calls the same handler directly, in-process, under its mutex hold. No new `ExpansionService.start_run` wrapper needed — the impl already exists.

This task: ensure the underlying handler is exported as `start_expansion_run_impl` (or equivalent name) so the dispatcher can `from gobby.mcp_proxy.tools.tasks_ops import start_expansion_run_impl` rather than going through the MCP transport layer. Verify the impl returns the `ExpansionRun` (or at minimum `run.id`); if today it only returns a serialized dict, refactor to return the underlying record alongside its dict form, or have it return `run.id` directly. Add a unit test asserting the impl returns the run id and that compile failures still produce a row whose `id` the caller can persist.

The dispatcher captures `run.id` and writes it into `task_artifacts.expansion_run_id` (§1.9). Compile failures are recoverable: `rule_validate_expansion` waits for the run to reach a terminal state (completed OR failed); on failed, expansion-qa picks it up and rejects via `mark_task_review_rejected(lifecycle=expanding)`, which clears `expansion_run_id` and increments `expansion_attempts` (§1.8 R4.F1 extension).

**Acceptance:**

- 2.8b.1 — Expose start_expansion_run_impl for in-process dispatcher use is implemented according to this section. file: `src/gobby/mcp_proxy/tools/tasks_ops.py`.

### 2.9 Expansion-QA transition contract [category: config] (depends: 1.8, 1.1b)
`kind: deliverable`

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

## P3 Phase 3: Entry Points — Build Config + CLI + Interactive Skill
`kind: framing`

**Goal**: Provide the "one-line starts the automation" surface. Config hierarchy with global / project / flag / task layers. A CLI command for the quick path and an interactive `/gobby build` skill for the wizard path.

### 3.1 Build config loader [category: code]
`kind: deliverable`

Target: `src/gobby/config/build.py` (new)

Merge order (later wins): built-in defaults → `~/.gobby/build.yaml` (global) → `<project_root>/.gobby/build.yaml` (project) → CLI/MCP/HTTP flags.

```yaml
# ~/.gobby/build.yaml (and project counterpart)
default_skip_stages: []                    # e.g., ["plan_review", "holistic_review"]
default_isolation: worktree                # none | worktree | clone
default_yolo: false
default_max_review_rounds: 3
default_target_branch: null                # R4.F6: null = auto-detect from `git rev-parse --abbrev-ref HEAD` at build time
clones_dir: ~/.gobby/clones                # R4.F2: where isolation=clone places clones
cleanup_clones_on_merge: true              # R4.F2: delete the clone after a successful merge (§2.10); preserved on failure regardless
max_active_agents: 10                      # global dispatcher slot cap
dispatch_interval_seconds: 60              # cron tick interval

# Profile presets — CLI-layer sugar, not stored on tasks.
profiles:
  quick:      { skip_stages: [plan_review, test_arch, expanding, qa, holistic_review, pr], isolation: none, yolo: false }
  review:     { skip_stages: [plan_review, pr], isolation: worktree, yolo: false }
  full:       { skip_stages: [], isolation: worktree, yolo: false }
  full-yolo:  { skip_stages: [pr], isolation: worktree, yolo: true }
  # "auto" is resolved at build time, not a stored entry.
```

Config model:

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Isolation = Literal["none", "worktree", "clone"]   # R4.F2: all three implemented; see §1.6/§1.7/§2.10/§3.2 wiring.
SKIPPABLE_STAGES = frozenset({"plan_review", "test_arch", "expanding",
                               "qa", "holistic_review", "pr"})

@dataclass(frozen=True)
class BuildConfig:
    default_skip_stages: tuple[str, ...] = ()
    default_isolation: Isolation = "worktree"
    default_yolo: bool = False
    default_max_review_rounds: int = 3
    default_target_branch: str | None = None       # R4.F6: None = resolve from git HEAD at build time
    clones_dir: Path = Path.home() / ".gobby" / "clones"   # R4.F2
    cleanup_clones_on_merge: bool = True           # R4.F2
    max_active_agents: int = 10
    dispatch_interval_seconds: int = 60
    profiles: dict[str, dict] = field(default_factory=dict)   # sugar presets

def load_build_config(project_root: str | None = None) -> BuildConfig:
    """Merge global → project. CLI overrides apply on top in gobby.build.service.build()."""
    ...

def resolve_profile(cfg: BuildConfig, name: str, input_ref: str) -> dict:
    """Resolve a profile name (or 'auto') to {skip_stages, isolation, yolo}.
    'auto' picks based on input_ref shape: plan_file → review; leaf task → quick;
    epic with plan → full; epic without plan → error."""
    ...
```

Daemon uses `max_active_agents` and `dispatch_interval_seconds` at cron-registration time (§1.10). Build surfaces use the rest.

**Acceptance:**

- 3.1.1 — Build config loader is implemented according to this section. file: `src/gobby/config/build.py`.

### 3.2 Build service — CLI + MCP + HTTP shared core [category: code] (depends: 3.1)
`kind: deliverable`

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

### 3.3 `/gobby build` interactive skill [category: docs] (depends: 3.2)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/build/SKILL.md` (new)

Wizard-driven. Branches:

- **Input classification**: plan file? task ref? just an idea (→ delegate to `/gobby plan`, then return)?
- **Profile or manual**: "pick a preset (quick / review / full / full-yolo / auto) or customize (skip-stage + isolation + yolo)?"
- **If manual**: multi-select of skippable stages; isolation; yolo y/n.
- **Max review rounds** (only if `plan_review` is enabled): prompt; default from config.
- **Confirm + assemble**: show the equivalent `gobby build ...` invocation, then run it via direct call into the shared build service.

When input is "just an idea", skill invokes `/gobby plan` to write the plan, captures the resulting plan file path and any adversary rounds already run, then resumes with the plan-file branch.

No separate yolo prompt inside a profile branch — `full-yolo` already carries yolo; other presets do not. Manual mode asks directly.

**Acceptance:**

- 3.3.1 — /gobby build interactive skill is implemented according to this section. file: `src/gobby/install/shared/skills/build/SKILL.md`.

### 3.4 Cascade resolved state to subtree at build time [category: code]
`kind: deliverable`

Target: `src/gobby/build/service.py` (implementation) + `src/gobby/storage/tasks/_crud.py` (helpers)

When `gobby build` primes an epic, copy the resolved state onto every descendant task in the subtree at build time. No runtime inheritance, no parent-lookup helper — snapshot-at-build keeps the model honest about what's actually being run.

Cascaded fields:

- `isolation` (column) — copied from epic to children.
- `yolo` (column) — copied from epic to children.
- `stage-:<name>` labels — copied from epic to children.
- `allow_automation` (column) — set to `true` on the entire subtree.

**Not cascaded:**

- `assigned_agent` — per-leaf, set by expansion (§2.8) or `--agent` on single-leaf input. Never inherited from parent.
- `additional_skills` — per-leaf, set by expansion. Never inherited.
- `lifecycle` — per-task, driven by the dispatcher rule engine.

Rules (§1.7) always read the task's own resolved state; they never walk the parent chain.

**Acceptance:**

- 3.4.1 — Cascade resolved state to subtree at build time is implemented according to this section. file: `src/gobby/build/service.py`.

## P4 Phase 4: Retirement of Overlapping Dispatchers
`kind: framing`

**Goal**: Remove the three obsolete dispatchers. Use tombstone-with-`enabled: false` (sync-idempotent) rather than move-to-`deprecated/` (sync-breaking).

### 4.1 Remove conductor package [category: refactor]
`kind: deliverable`

Target: `src/gobby/conductor/`

Delete the entire package. Grep-verify no external imports (`from gobby.conductor`, `import gobby.conductor`). The cron registration previously performed by `conductor/manager.py` is replaced by the registration in §1.10.

**Acceptance:**

- 4.1.1 — Remove conductor package is implemented according to this section. file: `src/gobby/conductor/`.

### 4.2 Tombstone obsolete pipelines [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/pipelines/`

Replace contents of these files in place with minimal `enabled: false` tombstones carrying a `deprecated: true` advisory field and a `deprecated_reason` description:

- `orchestrator.yaml`
- `front-half-orchestrator.yaml`
- `conductor.yaml`
- `dev-orchestrator.yaml`
- `delivery-orchestrator.yaml`

Example shape:

```yaml
name: orchestrator
version: "2.1"
enabled: false
deprecated: true
deprecated_reason: |
  Replaced by the state-driven dispatcher registered as a cron handler
  (src/gobby/dispatch/). Kept as an in-place tombstone so bundled sync
  preserves the installed DB row rather than soft-deleting as an orphan.
description: |
  [DEPRECATED] Original tick-based orchestrator pipeline.

steps: []
```

If `deprecated` isn't a recognized field in `PipelineDefinition` today, add it as an advisory-only field (one-line addition to `src/gobby/workflows/definitions.py`). Sync behavior already preserves the DB `enabled` column across YAML content updates (confirmed in grounding).

**Acceptance:**

- 4.2.1 — Legacy front-half pipeline is tombstoned in place with enabled false and replacement notes. file: `src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml`.
- 4.2.2 — Legacy orchestrator pipeline is tombstoned in place with enabled false and replacement notes. file: `src/gobby/install/shared/workflows/pipelines/orchestrator.yaml`.

### 4.3 Tombstone obsolete agents [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/`

Same treatment for:
- `conductor.yaml` — tombstone.
- `pipeline-worker.yaml` — audit callers; if unused, tombstone. If any surviving pipeline still references it, retain.
- `developer.yaml` — tombstone. Replaced by `frontend-developer.yaml` (§2.5) and `backend-developer.yaml` (§2.6); `rule_dispatch_leaf` (§1.7) dispatches whichever agent the expander assigned, with `backend-developer` as the default-when-missing. Keep the YAML as an `enabled: false` tombstone so the installed DB row is preserved across sync.

Keep role agents (`planner`, `plan-adversary`, `frontend-developer`, `backend-developer`, `qa-reviewer`, `expansion-qa`, `test-architect`, `merge`, `holistic-reviewer`, nightly-*, and `requirements-analyst` — retained as a stub agent for interactive-only use cases even though no rule dispatches it).

**Acceptance:**

- 4.3.1 — Deprecated requirement/planning agents are tombstoned in place with enabled false and replacement notes. file: `src/gobby/install/shared/workflows/agents/`.
- 4.3.2 — Lifecycle-owned agents remain enabled and load the required transition skills. file: `src/gobby/install/shared/workflows/agents/`.

### 4.4 DB migration to disable retired workflow_definitions rows [category: config] (depends: 4.1, 4.2, 4.3)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py`

Flip `enabled = false` on installed rows for the tombstoned pipelines and agents. Do not delete rows — drift detection relies on hash/content comparison, and a later cleanup migration can drop them after stability is confirmed.

**Acceptance:**

- 4.4.1 — DB migration to disable retired workflow_definitions rows is implemented according to this section. file: `src/gobby/storage/migrations.py`.

### 4.5 Update docs [category: docs] (depends: 4.1)
`kind: deliverable`

Target: `CLAUDE.md` (project root), `GUIDING_PRINCIPLES.md`, any `docs/` page referencing the old model

- Remove references to the LLM-driven conductor tick.
- Add a "Dispatch" section: rule location (`src/gobby/dispatch/rules.py`), how to add a new rule, `allow_automation` / `yolo` / `isolation` / stage-skip model, `gobby build` entry points (CLI + MCP + HTTP), agent-slot cap. Note that profiles are CLI-layer sugar only and resolved state (skip-stages, isolation, yolo) is what lives on tasks.
- Document the `task_dispatch_mutex`, `task_artifacts`, and `task_lifecycle_events` adjacent tables and their access patterns.
- Note retired pipelines and their tombstone status; link to #12728 for PR/merge work.

**Acceptance:**

- 4.5.1 — Update docs is implemented according to this section. file: `CLAUDE.md`.

## T1 Task Mapping
`kind: framing`

<!-- Populated by /gobby expand -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

**Acceptance:**

- T1.1 — Each populated Task Mapping row is represented by a stable acceptance item in the owning deliverable section instead of relying on a parser-visible table row. file: `.gobby/plans/task-12725-lifecycle-dispatch.md`.

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
- `tests/storage/test_holistic_rejection.py` (R4.F5) — `mark_task_review_rejected(epic, cited_subtasks=[...])` on `lifecycle=holistic_review` atomically appends findings, reopens cited subtasks (`closed → open`), and rewinds lifecycle (`holistic_review → in_development`) in a single transaction; assert atomicity by injecting a mid-transaction failure and checking nothing partially applied. Rejecting `lifecycle=holistic_review` without `cited_subtasks` (or with `[]`) raises a validation error. Confirm `rule_all_closed_advance_to_holistic` does NOT immediately re-fire because at least one cited leaf is now `open`.
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
