<!-- markdownlint-disable MD013 MD033 MD036 MD040 MD060 -->

# Lifecycle-State-Driven Agent Dispatch

> **Plan ID:** task-12725-lifecycle-dispatch-rev1

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
- **Lifecycle is dispatch-relevant on every task** (epics AND leaves). The shipped schema (#12776) puts the `lifecycle` column on `tasks` directly; both epics and leaves carry it. Epics walk the full pipeline (`plan_review → test_arch → expanding → in_development → holistic_review → pr → merging → merged`); leaves typically inhabit `in_development → holistic_review` as the dev/qa loop drives them, then park at `(holistic_review, review_approved)` until the parent epic merges and `rule_cascade_close_on_merge` cascades them to `closed`. Expansion (§2.8) initializes every generated leaf to `lifecycle=in_development` so `rule_dispatch_leaf` can pick it up; the legacy `lifecycle_stage` column (constraint: `in_progress | needs_review | review_approved`) is unrelated and unchanged. Helper predicates inspect `task.lifecycle` directly on both epics and leaves.
- **One cron heartbeat**, not per-epic. `CronExecutor.register_handler("state-dispatcher", handler)` registered on daemon startup; one interval cron job fires it every N seconds (N ≈ 30–60).
- **Single global agent-slot cap**, configurable. Default 10 concurrent active autonomous runs on this machine; other users tune down. Overflow queues for the next tick (no persistent queue — state-driven rules re-evaluate).
- **`plan_review ↔ planner` rewrite loop** is the only in-stage loop with adversary involvement. `dev ↔ qa` is a leaf-level loop (when QA is in the profile).
- **Stages are the atomic unit; profiles are CLI-layer sugar only.** The task model does **not** store `profile:<name>`. `gobby build --skip-stage a,b,c` (comma-separated) writes `stage-:<name>` labels onto the task; the dispatcher reads those. `--profile quick|review|full|full-yolo` is a shorthand at the CLI/MCP/HTTP layer that expands to the equivalent `--skip-stage` list plus `--isolation` and `--yolo`. Redefining a profile never changes existing tasks because the resolved state is snapshotted at build time. No `STAGE_BY_PROFILE` map in the dispatcher.
- **Skippable stages** are `{plan_review, test_arch, expanding, qa, holistic_review, pr}`. Non-skippable (`dev`, `worktree`, `merging`) raise a clear error if listed in `--skip-stage`.
- **Isolation is its own knob**: `--isolation none|worktree|clone`, default `worktree`. Written as an `isolation` column on the task. Separate from stage selection. All three modes implemented in this epic — `worktree` shares `.git` (cheap, fast); `clone` is an independent local clone (portable, hard-isolated, deletable sandbox); `none` runs in-branch on the source repo. Clone uses existing `CloneGitManager` / `LocalCloneManager` infrastructure (§1.6, §1.7, §1.9, §2.10).
- **Target branch is durable build-time state.** `gobby build` captures `--target-branch <name>` (default: `git rev-parse --abbrev-ref HEAD`) into `task_artifacts.target_branch` at invocation. `CreateWorktree` (§1.6/§1.7) and `merge.yaml` (§2.10) resolve from there — no rule hard-codes `main`. (R4.F6 fix.)
- **`yolo` means never escalate**, not "no PR." A yolo task's rules pick a deterministic fallback at every would-be-escalation site instead of `EscalateTask`. Yolo is safe because the worktree sandboxes failures. Yolo cascades at `gobby build` time onto the subtree, not at dispatch time.
- **Existing `merge.yaml` is only a merge runner, not a PR-creation agent.** The `pr` lifecycle stage has no existing agent. Until follow-up epic **#12728** ships a real PR-creation agent, non-yolo `rule_pr` escalates with `needs_human: PR creation not yet automated`; a human opens the PR, then calls the extended `de_escalate_task(task_id, target_status="review_approved", lifecycle=Lifecycle.merging)` (single call — §1.8). Yolo `rule_pr` skips straight to `merging` (no remote PR). Then `merge.yaml` runs. Clean merges complete; conflicts escalate (non-yolo) or are best-effort-resolved in-agent (yolo).
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
- **R7.F2** `ExpansionService(db)` and `LocalExpansionRunManager.create(task_id=, tdd=)` were both wrong shapes — real `ExpansionService(*, task_manager, llm_service, config=None, run_manager=None)` and `LocalExpansionRunManager.create(*, parent_task_id, project_id, triggering_session_id, input_source, plan_file=None, ...)`. Plus `compile_run` raises on failure rather than returning a failed-run record. **Fix**: §1.9 `StartExpansionRun` case now imports and calls `start_expansion_run_impl` from `gobby.mcp_proxy.tools.tasks._expansion` directly (the existing MCP tool's underlying handler, which already builds the right `LocalExpansionRunManager.create(...)` call against the real signatures). §2.8b reduced from "add a new wrapper" to "ensure the existing handler is exported as `start_expansion_run_impl` for in-process use." No new pseudo-APIs.
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

## Plan Changelog

`kind: framing`

Cumulative one-bullet-per-surgical-fix log of revisions made between adversary rounds (per §2.23.3). Each round's edits land here so the next adversary instance — spawned with fresh context per §2.23.1 — can see how the plan moved between rounds without rereading the full description.

**Round 2 (this revision, in response to Round 1 adversary findings F1–F5):**

- F1 (gobby-format) — Stripped dangling `(depends: …)` annotations whose targets do not exist in this trimmed revision: §1.3 (was `depends: 1.2`), §1.8 (was `depends: 1.1`), §2.8b (was `depends: 1.1b`), §2.9 / §2.10 (dropped `1.1b` from `depends: 1.8, 1.1b`), §2.15 (was `depends: 1.1`), §2.21 (was `depends: 1.1`), §3.2 (was `depends: 3.1`). The merged-foundation work that those refs originally pointed at lives in #12776/#12777/#12778 and is documented in the relevant sections' `Status: partial` notes; no in-plan dependency edges are needed for it.
- F2 (bad-sequencing) — Resolved the manifest deadlock between §2.21 and §2.22. `parse_plan` now accepts a `parse_mode` parameter (`draft | expansion | strict`). Plan-adversary review parses in `draft` mode (manifest optional); on clean review, the adversary writes the manifest and self-checks in `expansion` mode (manifest required and strictly validated); downstream `gobby expand` parses in `expansion` mode. §2.21.3 and §2.22.4 acceptance items reflect the mode contract.
- F3 (unhandled-edge) — Split validation gates between approval and rejection. `mark_task_review_approved` runs the full close-equivalent gates (commit-attached, validation_criteria pass, errors_resolved, memory_review_completed) — approving IS the close-equivalent path. `mark_task_review_rejected` and `escalate_task` run light gates (valid claim/session, `rejection_notes`/`reason` non-empty); rejection MUST succeed when validation fails or commits are missing because that is the state the reviewer is reporting. §1.8.6 and §2.14a.5 acceptance items spell out the verdict-split via a `_run_validation_gates(task, verdict)` helper.
- F4 (bad-sequencing) — Narrowed §2.20's `depends:` annotation from the entire §2.11–§2.24 range to `(depends: 2.21, 2.22)`. §2.20 now correctly reads as a pre-implementation gate that proves Epic 1's deterministic compile path can produce a covered task tree against this plan, rather than a late smoke test that fires after most of the §2.X work has already landed. Other §2.X sections only need their acceptance items reviewed by plan-adversary; their implementation is downstream of §2.20.
- F5 (traceability) — Resolved the conflict between §1.7 hard-coded constants and §2.19 `task_artifacts`-driven retry caps. §1.7 now defines `MAX_EXPANSION_ATTEMPTS_DEFAULT` and `MAX_QA_ROUNDS_DEFAULT` as fallbacks and introduces `_resolve_retry_cap(task, name, default)` that prefers `task_artifacts` (set by `gobby build` per §2.19) over the default. Rule code reads via the helper from the start, so §2.19's contribution becomes pure data plumbing (BuildConfig defaults, CLI flags, BuildOptions resolution, persistence) without rule-code edits. §1.7.2 and §2.19.4 acceptance items align around this single read path.

**Round 3 (this revision, in response to Round 2 adversary findings F1–F7):**

- F1 (gobby-format, table-row decomposition) — Decomposed the §2.19 retry-cap table (5 rows) and §2.21 parser-mode table (3 rows) into per-row acceptance items with stable IDs. §2.19 now has one item per cap (`max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`, `max_holistic_rounds`, `max_review_rounds`) plus cross-cutting items for resolution order and BuildResult shape; §2.21 splits 2.21.3 into 2.21.3 (parameter signature), 2.21.3a (draft mode), 2.21.3b (expansion mode), 2.21.3c (strict default), 2.21.3d (cross-mode invariants). Per-row coverage now intact under Plan-Coverage Contract.
- F2 (bad-sequencing, leaf state contract) — Updated the Constraints section: lifecycle is on every task (epics AND leaves) per the shipped #12776 schema, not epic-only as the stale prose claimed. Added §2.8.4 acceptance item requiring expansion to initialize generated leaves at `lifecycle=in_development`. Replaced every invalid `lifecycle_stage=holistic_review` reference with `lifecycle=holistic_review` (the legacy `lifecycle_stage` column's CHECK constraint only allows `in_progress | needs_review | review_approved`, so the prior prose would have been a runtime CHECK failure if implemented literally).
- F3 (bad-sequencing, approval gate over-scope) — Made `_run_validation_gates(task, verdict, lifecycle, *, caller_agent=None)` stage-aware. Three gate sets now: **leaf-close** (full close-equivalent: commit-attached, validation_criteria, errors_resolved, memory_review_completed) for qa-reviewer leaf approval at `lifecycle=in_development`; **stage-advance** (stage-specific output gates only: manifest emitted / test architecture appended / expansion run completed / leaves terminal-or-holistic / merge clean) for plan-adversary, test-architect, expansion-qa, holistic-reviewer, and merge stage approvals; **light** (claim/session + actionable rejection_notes/reason) for any rejection or escalation. §1.8.6 and §2.14a.5 acceptance items rewritten with the table.
- F4 (traceability, de_escalate_task API name) — Renamed `next_status` → `target_status` everywhere in the plan to match the live MCP schema (verified via `get_tool_schema(de_escalate_task)`). The `lifecycle` extension parameter is added as an additional optional parameter; the existing `target_status` is preserved.
- F5 (weak-testability, V1 verification) — Replaced the stale `tests/tasks/test_expansion_start_run.py::ExpansionService.start_run(...)` test with `tests/tasks/test_start_expansion_run_impl.py` (aligned with §2.8b — there is no `start_run` wrapper; the `start_expansion_run_impl` export is what the dispatcher imports). Updated `tests/build/test_target_branch.py` to assert leaf builds leave `task_artifacts.target_branch` UNSET (per §3.2 leaf builds running with `isolation=none`); plan-file and epic builds populate it.
- F6 (gobby-format, monolith target) — Added §2.8c "Split `expansion_service.py` monolith" as a refactor deliverable (`category: refactor`) that depends on §2.8 and §2.8b. Splits the 1,495-line file into focused modules under `src/gobby/tasks/expansion/` (≤ 800 lines per file). Backward-compat re-export shim preserved at the old path. Acceptance items 2.8c.1–2.8c.4 cover module structure, shim, internal-import migration, and test-suite stability.
- F7 (unhandled-edge, mutex detach state leak) — Replaced the task-id-global `_DETACHED: set[str]` with a per-acquire `_DETACH_TOKENS: dict[str, bool]`. `acquire()` now yields `(ok, token)`; `detach_from_context(token)` records under the token; the `finally` block pops the token before scope exit so subsequent acquires on the same task get a clean detach decision. Updated the §1.9 dispatcher caller to consume the tuple yield and pass the token. Added §1.4.2 acceptance item with the regression test scenario the prior design failed.

**Round 4 (this revision, in response to Round 3 adversary findings F1–F4):**

- F1 (bad-sequencing, dispatcher exhaustiveness) — Added match arms in §1.9 `_dispatch` for `MarkTaskReviewApproved`, `CascadeCloseLeaves`, and `ArchivePlan`; added `CreateClone` to the §1.9 imports; documented `_action_kind` exhaustive map (missing entry raises KeyError so adding a new action without updating the map fails loud). Added §1.9.2/1.9.4 acceptance items for the exhaustiveness contract.
- F2 (bad-sequencing, yolo merge gate deadlock) — Switched the §2.10 yolo cap-exhausted force-advance from `mark_task_review_approved` to `advance_lifecycle(epic, to=Lifecycle.merged, ...)`. The Round 3 stage-advance gate at `lifecycle=merging` requires merge-clean state by design (so non-yolo approvals can't slip past a failed merge); the yolo fallback bypasses that gate via the explicit transition tool, which §1.8 documents as the escape hatch. Same pattern applied to §1.7 `rule_qa` yolo cap-exhaustion (`AdvanceLifecycle(holistic_review, status="review_approved")` instead of `MarkTaskReviewApproved`) — the leaf-close gate would re-run the failing validation that triggered the cap-exhaustion. Updated `advance_lifecycle` signature to accept an optional `status` parameter for these explicit-target callers. Added §2.10.4 acceptance item + V1 verification updates.
- F3 (unhandled-edge, merging stage-skip path) — Removed the `_stage_enabled(task, "merging")` branches from `rule_merging` (`merging` is non-skippable per Constraints, so no skip path) and from `rule_all_leaves_holistic`'s target-chain (`Lifecycle.merging` is now the unconditional terminal in the chain — `Lifecycle.merged` is no longer reachable via stage-skip). Added defensive audit-marker handling in `rule_merging` if a `stage-:merging` label is observed despite §3.2 validation rejecting it at build time. Added §3.2.7 acceptance item for the build-service `--skip-stage merging` rejection (with `dev` and `worktree` covered too).
- F4 (bad-sequencing, _resolve_cwd agent_name signature) — Updated §1.9 `SpawnAgent` dispatch to call `_resolve_cwd(task, s.agent)` so the merge agent (`agent_name="merge"`) routes to the source repo regardless of `task.isolation`. Added §1.9.3 acceptance item with the dispatcher-path test as the contract gate (helper-unit tests are no longer the only check).

**Round 5 (this revision, in response to Round 4 adversary findings F1–F4):**

- F1 (bad-sequencing, CloseLeaf untracked) — Removed `CloseLeaf` from §1.6 actions and the `Action` union. No rule emits it; close transitions go through `mark_task_review_approved` (leaf-close gate, §1.8.6) or `CascadeCloseLeaves` (epic-merge cascade). Keeping it as an unused escape hatch undermined the §1.9.2 exhaustiveness contract.
- F2 (unhandled-edge, expansion failed-run path) — Renamed `_expansion_run_completed` to `_expansion_run_terminal` (returns true for `completed` OR `failed`). Updated `rule_validate_expansion` to dispatch expansion-qa on either terminal state. Updated §2.9 to specify expansion-qa reads failed-run details and rejects with the failure cited in `rejection_notes`, so `mark_task_review_rejected(lifecycle=expanding)` clears the artifact and increments attempts on failed runs (the prior `_completed`-only predicate stranded them). Helper list and prose updated accordingly.
- F3 (missing-requirement, pipeline deprecation classification) — Restructured §2.14b to handle BOTH `agents/deprecated/` AND `pipelines/deprecated/` as first-class. The retired orchestrators (`orchestrator.yaml`, `front-half-orchestrator.yaml`, `dev-orchestrator.yaml`, `delivery-orchestrator.yaml`, `conductor.yaml`-pipeline) are pipeline definitions, not agents — the prior plan misclassified them. Added new acceptance items §2.14b.3 (bundled-sync removes pipeline rows) and §2.14b.4/§2.14b.5 (migrations broken out by kind). The conductor AGENT (separate from the pipeline of the same name) goes to `agents/deprecated/`.
- F4 (weak-testability, AdvanceLifecycle.status field) — Added `status: str | None = None` field to the `AdvanceLifecycle` dataclass in §1.6. Updated §1.9 dispatcher's match arm to forward `status=status` to the `advance_lifecycle` helper. Updated §1.7 `rule_qa` yolo cap-exhaustion to pass `status="review_approved"` explicitly. The §1.8 `advance_lifecycle` signature already accepted the optional `status` parameter (added in Round 4); this round wires the action wrapper and dispatcher to actually carry it through.

**Round 6 (this revision, in response to Round 5 adversary findings F1–F5):**

- F1 (bad-sequencing, §1.7 missing CreateClone import) — Added `CreateClone` to §1.7's `from gobby.dispatch.actions import …` block so `rule_create_clone` resolves the symbol it returns. The §1.7 prose-imported action set is now consistent with what the rules emit.
- F2 (unhandled-edge, §1.9 TOCTOU on action class / agent_run_id) — Reworked `run_tick` to compute the pre-lock `_action_kind` for mutex acquisition only, then recompute the canonical action and kind AFTER the lock; if the kind differs, the dispatcher skips the task with an action-class-changed reason (the next tick re-acquires with the correct kind). `agent_run_id` is allocated from the FRESH action and attached to the mutex row via the new `mutex.attach_run_id(db, task_id, run_id, holder)` helper. Added §1.4.3 acceptance for `attach_run_id` and §1.9.5 acceptance for the TOCTOU-aware dispatch flow.
- F3 (gobby-format, §1.8 gate-set table-row decomposition) — Split the §1.8.6 acceptance item into §1.8.6 (helper signature + cross-product test) plus §1.8.6a (leaf-close gate set), §1.8.6b (stage-advance gate set), §1.8.6c (light gate set), one item per row in the gate-set table. Each carries its own test-name reference so the gate sets can be independently tracked and expanded.
- F4 (weak-testability, V1 missing failed-expansion-run dispatch test) — Updated `tests/dispatch/test_rule_expansion.py` description in V1 to require `test_rule_validate_expansion_dispatches_qa_on_failed_run` plus `test_failed_run_rejection_clears_artifact_and_increments_attempts`. The original failed-run stranding bug is now pinned by an explicit dispatch-on-failed test, not just the new `_expansion_run_terminal` predicate name.
- F5 (traceability, stale MarkTaskReviewApproved prose) — Rewrote §1.7 `rule_qa` docstring to state the yolo cap-exhaustion fallback emits `[AppendAuditMarker, AdvanceLifecycle(holistic_review, status="review_approved")]` (no longer mentions `MarkTaskReviewApproved`). Updated §1.9's `MarkTaskReviewApproved` dispatch-arm comment to clarify "no rule currently emits this — retained for forward compatibility" and removed the contradictory claim that `rule_qa` is the caller.

**Round 7 (this revision, in response to Round 6 adversary findings F1–F5):**

- F1 (bad-sequencing, §1.7 missing Isolation import) — Added `Isolation` to the `from gobby.storage.tasks._models import …` line in §1.7 alongside `Lifecycle, Task`. `rule_create_worktree`, `rule_create_clone`, and `rule_dispatch_leaf` reference `Isolation.worktree | clone | none`; the import block now matches what the rules use.
- F2 (bad-sequencing, expansion input_source mismatch with live API) — Aligned §1.9 dispatcher with the live `LocalExpansionRunManager.create` API: the `input_source` literal accepts only `"plan"` or `"task"` (NOT `"plan_file"` or `"epic"`). The plan-file path is passed via the separate `plan_file=` kwarg. Updated the dispatcher snippet to compute `input_source="plan" if _has_plan_file else "task"` and pass `plan_file=_plan_file_path(...)` when present.
- F3 (unhandled-edge, sweep_on_startup running per-tick) — Removed `sweep_on_startup` call from `run_tick`. Daemon-boot mutex reconciliation now lives in a one-shot `sweep_mutexes_once(db)` call in `runner_init.py`, executed once before any tick (cron or `_kick_dispatcher_tick`) can fire. This closes the overlapping-tick race where the second tick would delete a live non-spawn mutex row held by the first tick. Removed `swept` field from `TickReport` (no longer per-tick state).
- F4 (traceability, stale MarkTaskReviewApproved prose) — Rewrote §1.6 `MarkTaskReviewApproved` dataclass docstring to clarify "no rule currently emits this — retained for forward compatibility." Rewrote §1.8 `lifecycle=merging` rejection prose to name `append_description_section` + `advance_lifecycle(... to=Lifecycle.merged ...)` as the merge yolo cap-exhausted force-advance, matching §2.10. No prose path now claims `mark_task_review_approved` is the yolo force-advance.
- F5 (bad-sequencing, §1.8 holistic-approval merging-skip path) — Removed the "or `merged` if both are skipped" wording from §1.8's holistic_review approval transition. The path is now: `holistic_review → pr` (default), or `holistic_review → merging` (when `pr` is skipped). NEVER `holistic_review → merged` via stage-skip. `merging` is non-skippable per Constraints; the merge agent always runs.

**Round 9 (this revision, in response to Round 8 adversary findings F1–F3):**

- F1 (bad-sequencing, rule_start_expansion races terminal runs) — Replaced the `not _expansion_active(task)` predicate with `_expansion_run_artifact_absent(task)` in `rule_start_expansion`. The prior predicate matched on terminal (completed/failed) runs, letting the start rule overwrite the terminal run before `rule_validate_expansion` could dispatch expansion-qa to read failure details and reject. Guarding on a MISSING `expansion_run_id` artifact preserves the recovery path: terminal runs keep their artifact until expansion-qa rejects (clearing it + incrementing `expansion_attempts`); only THEN does `rule_start_expansion` re-fire. Updated the helpers paragraph to reference the new predicate name.
- F2 (bad-sequencing, start_expansion_run_impl as registry closure) — Pinned an explicit dependency-injected signature for `start_expansion_run_impl` in §2.8b: `async def start_expansion_run_impl(*, task_manager, llm_service, config, completion_registry, triggering_session_id, task_id, plan_file=None, auto_apply=True, force_new=False, provider=None, model=None, project=None) -> ExpansionRun`. The MCP closure inside `RegistryContext` resolves dependencies from context and forwards to the impl; the dispatcher (§1.9) constructs them from the daemon services it already holds. Added §2.8b.2 acceptance for the dispatcher dependency wiring. Updated §1.9's `StartExpansionRun` match arm to pass all five dependencies explicitly.
- F3 (unhandled-edge, merge cleanup-after-approval orphans artifacts on cleanup failure) — Reordered §2.10's clean-merge success path so cleanup runs BEFORE terminal approval. Sequence: (1) `mark_worktree_merged` / `delete_worktree` / `clear_isolation_pair` (worktree) or `delete_clone` / `clear_isolation_pair` (clone) → (2) `mark_task_review_approved` (LAST). On any cleanup-step failure, the agent escalates with `reason="needs_human:merge_cleanup_failed:<step>:<details>"` and the epic stays at `lifecycle=merging, status=escalated` so a human can recover via `de_escalate_task(target_status='open', lifecycle=Lifecycle.merging, ...)`. Updated §2.10.2 acceptance and the relevant V1 verification entries to assert the new order. Approval can no longer succeed before cleanup, so terminal lifecycle + orphaned artifacts is unreachable.

**Round 10 (this revision, in response to Round 9 adversary findings F1–F2):**

- F1 (bad-sequencing, DispatcherServices wiring incomplete in §1.10) — Made the dispatcher's daemon-side state explicit end-to-end via a new `DispatcherServices` dataclass (db, task_manager, llm_service, config, completion_registry, triggering_session_id). `run_tick(services, holder, max_active)` now takes the container instead of just `(db, ...)` and reads dependencies from it; `_dispatch(services, ...)` does the same. `register_state_dispatcher(executor, services, config)` accepts the container; `runner_init.py` constructs it once at boot from the daemon services it already holds, stores it on `daemon_state.dispatcher_services`, and reuses it for both cron handlers and immediate `_kick_dispatcher_tick` (§3.2.5) — both surfaces wire expansion-impl identically. Updated §1.9, §1.10, §3.2.5 acceptance items. No more private global helpers like `_task_manager(db)` or `_llm_service()`; every dependency arrives at `run_tick` via `services`.
- F2 (unhandled-edge, yolo cleanup-failure escalates instead of force-advancing) — Split §2.10's cleanup-failure handling by yolo. Non-yolo keeps the `escalate_task("needs_human:merge_cleanup_failed:...")` path (lifecycle stays `merging, status=escalated` for human recovery). Yolo gets a deterministic fallback per the top-level "yolo never escalates" invariant: `[append_description_section("Yolo Fallbacks", ...), advance_lifecycle(to=Lifecycle.merged, reason="yolo: merge cleanup failed; force-advanced with artifacts preserved", by_actor="merge")]`. Artifacts stay uncleared on both branches for human inspection. Updated §2.10.2 acceptance + the merge-yaml prose; tests assert yolo cleanup-failure NEVER calls `escalate_task`.

**Round 11 (this revision, in response to Round 10 adversary findings F1–F3):**

- F1 (bad-sequencing, §1.10 / §3.2.5 cite non-live config/storage APIs) — Aligned the dispatcher wiring with three live APIs verified against source: (i) dispatch knobs live on `BuildConfig.max_active_agents` and `BuildConfig.dispatch_interval_seconds` (`src/gobby/config/build.py:68-69`), NOT on `DaemonConfig.get(...)` — `DaemonConfig` is a Pydantic model with no `.get` method. (ii) `ServiceContainer` (`src/gobby/app_context.py:30`) is the live dependency surface; `runner_init.py` adds a new `dispatcher_services: DispatcherServices` field on it, and `_kick_dispatcher_tick` (§3.2.5) reads via `get_app_context().dispatcher_services`. (iii) `cron_jobs` rows are created via `CronJobStorage.create_job(..., schedule_type="interval", interval_seconds=..., action_config={"handler": "state-dispatcher"})` — there is no `schedule_expression` field. Updated the §1.10 code snippet, the cron-row example, §1.10.1, added §1.10.2 (ServiceContainer wiring) and §1.10.3 (BuildConfig read-path), and updated §3.2.5 to call out the live wiring surfaces.
- F2 (weak-testability, V1 stale cleanup-failure escalation prose) — Updated the V1 entries for `tests/dispatch/test_merge_integration.py` and `tests/agents/test_merge_integration.py` to split cleanup-failure assertions by yolo: non-yolo escalates with `needs_human:merge_cleanup_failed:`; yolo NEVER calls `escalate_task`, instead emits `append_description_section + advance_lifecycle(to=merged, ...)` and preserves artifacts. The R10.F2 prose now matches §2.10.2's contract; expansion can no longer generate tests for the pre-Round 10 unconditional escalation.
- F3 (nit, manual smoke command points at archived plan) — Updated step 2 of the V1 manual smoke from `task-12725-lifecycle-dispatch.md` (which moved to `completed/` during pre-revision cleanup) to `task-12725-lifecycle-dispatch-rev1.md`.

**Round 12 (this revision, in response to Round 11 adversary findings F1–F2):**

- F1 (bad-sequencing, `_kick_dispatcher_tick` lacked live BuildConfig surface) — Added `build_config: BuildConfig` as a first-class field on `DispatcherServices`. `runner_init.py` calls `load_build_config(runner.database, runner.project_id)` and passes the result into the dataclass. `_kick_dispatcher_tick` reads `services.build_config.max_active_agents` directly — never `get_app_context().config.max_active_agents` (that field is `DaemonConfig`, no `.get()`). Updated §1.9 dataclass definition, the §1.10 wiring snippet, and §3.2.5 acceptance to reflect the new field.
- F2 (bad-sequencing, expansion impl path + class name don't exist live) — Verified live source: there is no `gobby.mcp_proxy.tools.tasks_ops` module; the expansion handler lives at `gobby.mcp_proxy.tools.tasks._expansion`. Replaced every reference (§1.9 dispatcher import, §2.8b acceptance file paths, V1 test paths) via `replace_all`. Also corrected `CompletionRegistry` → `CompletionEventRegistry` (the live class at `src/gobby/events/completion_registry.py:21`) — both in the `start_expansion_run_impl` signature and in the `DispatcherServices` field type. The dispatcher's `from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl` import is now valid against the actual repo layout.

**Round 13 (this revision, in response to Round 12 adversary findings F1–F2):**

- F1 (bad-sequencing, `load_build_config` signature + missing `runner.dispatcher_session_id`) — Verified live: `load_build_config(project_root: str | Path, flag_overrides: Mapping[str, Any] | None = None)` is path-based, NOT `(db, project_id)`. Updated §1.10 wiring snippet to call `load_build_config(runner.git_manager.repo_path)` and explained that flag overrides are per-build (persisted to task_artifacts via §2.19) so the daemon-side load uses none. Added §1.10.4 acceptance: synthetic dispatcher session is registered exactly once at daemon boot with `source="dispatcher"`, `external_id="dispatcher-<machine_id>-<boot_timestamp>"`, exposed as `runner.dispatcher_session`; `triggering_session_id` is `runner.dispatcher_session.external_id`. Added test acceptance for source value, single-row creation, and "never claims tasks" invariant.
- F2 (bad-sequencing, §1.9 missing dependency on §2.8b export) — Added `2.8b` to §1.9's `depends:` list. Added a cross-phase dependency note in §1.9 explaining that §1.9 lives in Phase 1 but depends on §2.8b in Phase 2 because the dispatcher's `StartExpansionRun` handler imports `start_expansion_run_impl` (§2.8b's deliverable). The deterministic compile path now schedules §2.8b's TDD leaves before §1.9's `StartExpansionRun`-handler TDD leaf.

**Round 14 (this revision, in response to Round 13 adversary findings F1–F3):**

- F1 (bad-sequencing, `executor` not in scope in `init_servers`) — Added §1.10.5 acceptance: `GobbyRunner` exposes `runner.cron_executor` (the same `CronExecutor` instance attached to `runner.cron_scheduler`) so dispatcher wiring in `init_servers` can call `register_state_dispatcher(runner.cron_executor, ...)`. Updated the §1.10 wiring snippet to reference `runner.cron_executor`.
- F2 (bad-sequencing, `register_session` returns str not Session) — Verified live: `LocalSessionManager.register_session(...) -> str` (the persisted session id, or temporary UUID on failure). Updated §1.10.4 + the §1.10 wiring snippet to store the SAME string we passed as `external_id` on `runner.dispatcher_session_id` (a string attribute, NOT a Session object). `DispatcherServices.triggering_session_id` reads that string directly. Added `dispatcher_session_id: str` attribute spec on `GobbyRunner`.
- F3 (bad-sequencing, no deliverable creates `gobby.dispatch.prompts` / `PROMPT_BUILDERS`) — Added §1.6a as a new `[category: code]` deliverable: `src/gobby/dispatch/prompts.py` defines `PROMPT_BUILDERS: dict[str, PromptBuilder]` with all eight keys emitted by §1.7 rules (`planner_rewrite`, `plan_adversary`, `test_architect`, `expansion_qa`, `developer`, `qa_reviewer`, `holistic_reviewer`, `merge_runner`). §1.7's depends list now includes `1.6a`. Bijection-invariant test (§1.6a.2) introspects `RULES` for all `SpawnAgent.prompt_builder` literals and asserts the set equals `PROMPT_BUILDERS.keys()`, so adding a new spawn rule without a matching builder fails CI.

**Round 15 (this revision, in response to Round 14 adversary findings F1–F2):**

- F1 (bad-sequencing, SpawnRequest deps still reference private global helpers) — Extended `DispatcherServices` with three more fields: `session_manager: ChildSessionManager`, `machine_id: str`, `parent_session_id: str` (the dispatcher session UUID, same value as `triggering_session_id` since the dispatcher is the parent for all dispatched agents). Rewrote §1.9's `_dispatch(SpawnAgent)` arm to read every SpawnRequest dependency from `services.<field>` — no `_dispatcher_parent_session()`, no `_session_manager()`, no `_machine_id()`. Updated §1.10 wiring snippet to populate the new fields from `runner.session_manager` and `runner.machine_id`. Updated §1.10.2 acceptance to lock the no-private-helpers contract via a `gcode search-content` regression check. Cleaned the §2.8b.2 acceptance prose to read from `services.<field>` instead of stale private helpers.
- F2 (bad-sequencing, register_session storage UUID vs external_id) — Verified live: `LocalSessionManager.register_session(...) -> str` returns the **storage UUID** (the `sessions(id)` row primary key), NOT the `external_id` we passed in. The expansion_runs table foreign-keys `triggering_session_id` to `sessions(id)`, so propagating the external_id (per the prior Round 13 fix) would corrupt the FK. Updated §1.10 wiring snippet, §1.10.2, and §1.10.4 to capture the **return value** of `register_session(...)` and store it on `runner.dispatcher_session_id: str`. Added an explicit failure-path acceptance: if the returned id is not present in `sessions(id)` (the live API's "temporary UUID on failure" case), daemon startup raises rather than continue with a dangling FK. Removed every `runner.dispatcher_session.external_id` reference; the storage UUID alone is what threads through `DispatcherServices`.

**Round 16 (this revision, in response to Round 15 adversary findings F1–F2):**

- F1 (gobby-format, manifest heading lacks section ID) — Renamed the manifest heading from `## Task Manifest` → `## M1 Task Manifest`. The `M1` ID matches the canonical heading regex's alpha-prefix-plus-digit branch (same shape as the existing `P1`, `V1`, `O1` framing headings in this plan). Without an ID, the post-approval `parse_plan(..., parse_mode="expansion")` self-check (§2.22.4) would mechanically reject the manifest the adversary just wrote. Added explicit prose stating the parser treats `kind: manifest` as exempt from the `**Acceptance:**` requirement (manifest entries take the place of acceptance items for that section). Updated §2.21 Section shape, §2.21 Contract additions, and §2.22.1 acceptance to use the new `M1` heading.
- F2 (missing-requirement, plan-coverage.md not updated for §2.15/§2.16 retirement work) — Added §2.15.7 acceptance: replace the "Plan index" subsection in `docs/contracts/plan-coverage.md` with a DB-backed "Plan Storage" subsection (the `plans` table is authoritative; `gobby-plans` MCP/CLI are the read/write surfaces; `state` enum is `active | archived` only — no `merged`). Added §2.16.6 acceptance: remove the "Grandfathered plans" subsection, all `.legacy-classification.yaml` references, and `legacy` from the `plan_kind` enum docs. Both items pin a regression test (`tests/docs/test_plan_coverage_contract_sync.py`) that greps the contract file for stale tokens — no `index.yaml`, no `merged` state, no `legacy` plan_kind, no `.grandfathered`, no `.legacy-classification`. The canonical contract doc ends up describing only the post-revision implementation reality.

**Round 17 (this revision, in response to Round 16 adversary findings F1–F2):**

- F1 (unhandled-edge, plan-review bypass paths skip manifest emission) — Two manifest gates added: (a) `--skip-stage plan_review` is build-time validated against existing `## M1 Task Manifest` per new §3.2.8 acceptance — the build service runs `parse_plan(plan_path, parse_mode="expansion")` and rejects with a clear `BuildError` if the manifest is missing/malformed. By the time the dispatcher sees the task, the manifest invariant holds, so `rule_plan_adversary`'s skip-path can advance directly. (b) Yolo round-budget-exhausted path: introduced new `EmitStubManifest` action (added to §1.6 dataclasses, the `Action` union, the §1.7 imports, the §1.9 imports + dispatch arm). The yolo cap-exhausted fallback now emits `[AppendAuditMarker, EmitStubManifest, AdvanceLifecycle(test_arch)]` — a deterministic stub manifest synthesized from the plan's `kind: deliverable` sections preserves the §2.21 manifest invariant while keeping the "yolo never escalates" invariant. Audit marker captures the override.
- F2 (bad-sequencing, missing planner resubmit terminal contract) — Added §2.23.5 acceptance pinning the planner's terminal MCP transition: a single atomic call to `mark_task_needs_review(task_id=anchor_id, review_notes=...)` whose tool implementation clears `planning-current-verdict:rejected` and sets `status=needs_review` in one transaction. Planner's `allowed_mcp_tools` surface includes `mark_task_needs_review` + `escalate_task` only — NOT `mark_task_review_approved`, `close_task`, or `mark_task_review_rejected`. Updated the §1.8 `mark_task_needs_review` documentation to describe the atomic clear-and-set contract (R7.F-planner-resubmit) and pointed cross-reference to §2.23.5. Added a dispatch-flow test asserting the full rejection → planner rewrite → adversary re-review walk has no intermediate state where `rule_plan_rewrite_on_reject` could re-fire and trap the task.

**Round 18 (this revision, in response to Round 17 adversary findings F1–F2):**

- F1 (weak-testability, EmitStubManifest action coverage gaps) — Added `EmitStubManifest` to `_action_kind` (maps to `"field"` — same TTL/contention class as `AppendAuditMarker` since it's a file-side-effect action), added it to §1.9.2's exhaustive action list and dispatcher-test downstream-effect assertions (asserting `gobby.plans.manifest_emitter.emit_stub_manifest` is called with the plan file path). Updated the V1 `tests/dispatch/test_rules.py` `rule_plan_adversary` rounds-exhausted bullet to assert the exact `[AppendAuditMarker, EmitStubManifest, AdvanceLifecycle(test_arch)]` action sequence for the yolo path.
- F2 (unhandled-edge, EmitStubManifest failure-path gaps + §2.22.4 yolo-self-check escalation) — Tightened `EmitStubManifest`'s contract in §1.6 with R7.F-stub-manifest-strict: (a) missing `plan_file_path` artifact is a hard `RuntimeError` that prevents the subsequent `AdvanceLifecycle`, (b) existing-manifest validation runs `parse_plan(parse_mode="expansion")` and replaces a malformed manifest rather than no-op'ing past it, (c) post-write strict parse verification gates the lifecycle advance. Updated §1.9 dispatch arm to raise on missing artifact. Updated §2.22.4 (R7.F-self-check-yolo) to split self-check exhaustion by yolo: non-yolo escalates as before; yolo writes a `Yolo Fallbacks` audit marker, falls back to the deterministic stub manifest, and if even that fails appends a second audit marker and force-approves with explicit notes documenting the impossibility. yolo NEVER calls `escalate_task`.

**Round 19 (this revision, in response to Round 18 adversary findings F1–F2):**

- F1 (traceability, no deliverable for `manifest_emitter.py`) — Added §2.21a as a new `[category: code]` deliverable for `src/gobby/plans/manifest_emitter.py`. Pinned the public signature `emit_stub_manifest(plan_path, *, by_actor, plan_kind) -> EmitOutcome` where `EmitOutcome = Literal["fresh" | "replaced_malformed" | "noop_existing_valid" | "fallback_force_approve"]`. Six acceptance items cover the import surface, fresh emission, idempotent no-op, malformed-manifest replacement, absorbed-failure (never-raises) on unsalvageable plans, and default agent/tdd/category mapping. Updated §2.22's depends list to include `2.21a`.
- F2 (unhandled-edge, yolo livelock on stub-manifest failure) — Reframed `EmitStubManifest`'s contract (R7.F-no-livelock) so the dispatch arm NEVER raises. The §2.21a emitter absorbs every failure mode (returns one of four `EmitOutcome` values; on unsalvageable plans appends a `## Yolo Fallbacks` audit section to the plan file and returns `"fallback_force_approve"`). The dispatcher's `EmitStubManifest` arm: on missing `plan_file_path` artifact, appends an audit marker on the planning anchor and continues; on present artifact, calls the emitter and continues. The subsequent `AdvanceLifecycle(test_arch)` in the rule's action sequence ALWAYS runs — no path can leave the task at `plan_review` after a yolo cap-exhausted dispatch. Documented downstream `gobby expand` rejection at expansion time as the documented escape hatch for genuinely-unsalvageable plans (matches §2.22.4's force-approve fallback contract).

**Round 20 (this revision, in response to Round 19 adversary findings F1):**

- F1 (traceability, three of five §2.19 retry caps not wired into runtime paths) — Wired all five caps end-to-end:
  - **`max_review_rounds`** (R7.F-review-cap-wired): updated `_rounds_remaining` helper docstring to read via `_resolve_retry_cap(task, "max_review_rounds", MAX_REVIEW_ROUNDS_DEFAULT)` against `planning-round:N` label as the live counter. Added `MAX_REVIEW_ROUNDS_DEFAULT = 3` constant.
  - **`max_merge_attempts`** (R7.F-merge-cap-wired): `rule_merging` resolves the cap and threads it into the merge agent's `initial_variables` as `max_merge_attempts: int`. §2.10 prose updated to read the cap from session vars rather than hardcoded "default 3". Added `MAX_MERGE_ATTEMPTS_DEFAULT = 3` constant.
  - **`max_holistic_rounds`** (R7.F-holistic-cap-wired): added `_holistic_attempts` helper (reads `holistic-attempts:N` label on the epic) and a cap-exhaustion branch in `rule_holistic` symmetric to `rule_qa`'s pattern — non-yolo escalates with `needs_human:`; yolo emits `[AppendAuditMarker, AdvanceLifecycle(pr or merging)]` (force-advance bypassing stage-advance gate). §1.8's `mark_task_review_rejected(epic, lifecycle=holistic_review)` branch now increments `holistic-attempts:N` atomically with the cited-subtasks rewind. Added `MAX_HOLISTIC_ROUNDS_DEFAULT = 3` constant. holistic-reviewer agent receives the resolved cap + current attempt via `initial_variables` for use in signoff context.
  - All three constants land alongside the existing `MAX_EXPANSION_ATTEMPTS_DEFAULT` and `MAX_QA_ROUNDS_DEFAULT` so adding a new cap is a single-line module edit + one §2.19 acceptance item.

**Round 8 (this revision, in response to Round 7 adversary findings F1–F4):**

- F1 (bad-sequencing, isolation handler constructor arg order) — Verified the live `src/gobby/agents/isolation.py` signatures via direct source inspection: `WorktreeIsolationHandler(git_manager, worktree_storage)` and `CloneIsolationHandler(clone_manager, clone_storage, git_manager=None)` — both take the git/clone manager FIRST, then the storage. Plan previously documented them with the args swapped (which would have caused `prepare_environment` to call `.create_worktree(...)` on the storage object). Updated §1.9's `_build_isolation_handler` prose to match the live order; deleted the conflicting follow-up sentence; added the `tests/dispatch/test_dispatcher_isolation_handler_args.py` reference that locks the order.
- F2 (bad-sequencing, `db.transaction(immediate=True)` doesn't exist) — Verified live `LocalDatabase` API: `transaction()` and `transaction_immediate()` are separate parameter-less methods; there is no `transaction(immediate=True)`. Replaced every occurrence in §1.4 (mutex acquire) with `db.transaction_immediate()` so the mutex code is callable as written.
- F3 (bad-sequencing, `start_expansion_run_impl` doesn't accept `tdd`) — Removed the `tdd: bool` field from the `StartExpansionRun` action dataclass and from the dispatcher call. The deterministic compiler driven by the `## Task Manifest` (§2.21) decides which deliverables emit TEST/IMPL/REF triples via each manifest entry's `tdd: bool`; the dispatcher no longer needs a coarse epic-level toggle. Updated `rule_start_expansion` to emit `StartExpansionRun(task_id=task.id)` with no second argument; updated the dispatcher's match arm to call `start_expansion_run_impl(task_id, plan_file, auto_apply, force_new)` per the live MCP-tool signature.
- F4 (unhandled-edge, non-yolo merge conflict bypassing cleanup) — Updated §2.10's non-yolo conflict escalation recovery: the human resolves the conflict and calls `de_escalate_task(task_id, target_status='open', lifecycle=Lifecycle.merging, reason='...')` so the task routes back through `rule_merging` and the merge agent's normal success cleanup runs (mark_worktree_merged, delete_worktree, clear_isolation_pair, terminal write-back). The prior `lifecycle=Lifecycle.merged` recovery skipped cleanup and left orphaned artifacts. Updated the V1 verification entry to assert the escalate reason names `lifecycle=Lifecycle.merging`.

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

### 1.3 Extend task CRUD for new fields and helpers [category: code]

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

    R7.F-mutex-detach fix: detach state is **per-acquire** (token-scoped),
    not task-id global. Each successful acquire allocates a new token; only
    that token's owner can detach the mutex. After the `try`/`finally` runs,
    the token is dropped — a subsequent acquire on the same task gets a
    fresh token and a fresh detach decision. This closes the leak where
    a spawn-detach left `_DETACHED[task_id]` set forever, causing the next
    non-spawn acquire to skip its required scope-exit release.

    For kind='spawn', the caller should call detach_from_context(token) if
    the spawn succeeds — release then happens via the claim/end hooks (§1.5).
    For other kinds, the caller MUST NOT call detach; release happens
    automatically on context exit.
    """
    ttl = TTL_BY_KIND[kind]
    now = datetime.now(UTC)
    expires = (now + timedelta(seconds=ttl)).isoformat()
    acquired = False
    token = uuid4().hex  # per-acquire detach token; never reused across calls
    with db.transaction_immediate() as conn:  # BEGIN IMMEDIATE
        # UPSERT against task_dispatch_mutex (§1.1a). Absent row = free.
        row = conn.execute(
            "SELECT lease_until, run_id FROM task_dispatch_mutex WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is not None:
            lease_until, existing_run = row
            if existing_run is not None:
                yield (False, None)
                return
            if lease_until is not None and lease_until >= now.isoformat():
                yield (False, None)
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
        yield (True, token)
    finally:
        # R3.F5 fix: release on scope exit whenever we acquired and were not
        # detached, regardless of kind. R7.F-mutex-detach fix: detach is checked
        # by token, not by task_id, and the token entry is consumed before exit
        # so a subsequent acquire on the same task gets a clean detach state.
        if acquired:
            detached = _DETACH_TOKENS.pop(token, False)
            if not detached:
                with db.transaction() as conn:
                    conn.execute(
                        "DELETE FROM task_dispatch_mutex "
                        "WHERE task_id = ? AND lease_holder = ?",
                        (task_id, holder),
                    )

# Module-private map: detach token -> True. Populated only by detach_from_context
# under the matching acquire() scope; popped by acquire's finally so entries
# never outlive their owning acquire scope. This replaces the prior task-id-global
# `_DETACHED: set[str]` which leaked detach state across separate acquires.
_DETACH_TOKENS: dict[str, bool] = {}

def detach_from_context(token: str) -> None:
    """Tell the owning `acquire()` NOT to release on scope exit — handoff to
    the agent-run lifecycle. Called after a successful spawn only, with the
    token returned by `acquire()`. Tokens are per-acquire and consumed on
    scope exit; calling with a stale or unknown token is a no-op (defensive).

    Caller pattern (R7.F-toctou-action-class):
        with mutex.acquire(db, task_id, holder, kind) as (ok, token):
            if not ok:
                return
            # ... locked re-eval; on SpawnAgent action, allocate run_id ...
            attach_run_id(db, task_id, run_id, holder)
            run = execute_spawn(...)
            mutex.detach_from_context(token)  # only on spawn success
    """
    _DETACH_TOKENS[token] = True

def attach_run_id(db: LocalDatabase, task_id: str, run_id: str, holder: str) -> None:
    """Attach `run_id` to the mutex row owned by `holder` for `task_id`.
    R7.F-toctou-action-class: the dispatcher acquires the mutex BEFORE
    knowing which `agent_run_id` will be used (the canonical action is
    determined by the locked re-evaluation, not the pre-lock evaluation).
    On `SpawnAgent` paths, the run id is allocated from the fresh action
    after the lock is held; this helper writes it onto the existing row.
    UPDATE-only: requires the row to already exist with the matching
    holder; raises if the row was swept (defensive)."""
    with db.transaction() as conn:
        rows = conn.execute(
            "UPDATE task_dispatch_mutex SET run_id = ? "
            "WHERE task_id = ? AND lease_holder = ? AND run_id IS NULL",
            (run_id, task_id, holder),
        )
        if rows.rowcount == 0:
            raise RuntimeError(
                f"attach_run_id: no matching mutex row for task={task_id} holder={holder}"
            )

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
- 1.4.2 — Detach state is per-acquire (token-scoped), not task-id global. `acquire()` yields `(ok: bool, token: str | None)`; `detach_from_context(token)` records under the token; the `finally` block pops the token before exit so subsequent acquires on the same task get a clean detach decision. test: `tests/dispatch/test_mutex.py::test_detach_does_not_leak_across_acquires` covers the scenario the prior global-state design failed: (a) acquire kind="spawn" → detach_from_context(token1) → exit; (b) hook clears the DB row; (c) acquire kind="lifecycle" on the same task_id → exit without detach → assert the DB row is DELETED on scope exit (would have been retained under the leaked-detach design). Also covers (d) calling `detach_from_context` with an unknown/stale token is a no-op (defensive).
- 1.4.3 — `attach_run_id(db, task_id, run_id, holder)` UPDATEs the existing mutex row's `run_id` to support the dispatcher's TOCTOU-aware run-id allocation pattern (the dispatcher acquires the lock before knowing which run_id will be used; on `SpawnAgent`, the id is allocated from the locked re-evaluation and attached after the fact). The helper requires the row to exist with the matching holder and `run_id IS NULL`; mismatch raises. file: `src/gobby/dispatch/mutex.py`. test: `tests/dispatch/test_mutex.py::test_attach_run_id` covers (a) successful attach updates the row, (b) attaching to a missing row raises, (c) attaching to a row with non-null run_id raises, (d) attaching with mismatched holder raises.

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
    """R7.F-tdd-removed: `tdd` field removed. The deterministic compiler (driven
    by the `## Task Manifest` per §2.21) decides which deliverables emit
    TEST/IMPL/REF triples via each manifest entry's `tdd: bool`. The dispatcher
    no longer needs a coarse epic-level toggle, and the live
    `start_expansion_run_impl` API does not accept a `tdd` kwarg."""
    task_id: str

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
    status: str | None = None                        # explicit status override; when None, advance_lifecycle resolves
                                                     # a default per (to, task_type): merged→closed; leaf→holistic_review→review_approved;
                                                     # everything else→open. Yolo cap-exhaustion paths (§1.7 rule_qa,
                                                     # §2.10 merge force-advance) pass an explicit value to bypass the
                                                     # review-style gates the leaf-close / stage-advance gate sets would run.

# CloseLeaf was previously defined here as an escape hatch but no rule emits
# it; close transitions go through `mark_task_review_approved` (leaf-close
# gate set, §1.8.6) or the cascade close on epic merge (`CascadeCloseLeaves`).
# Removed from §1.6 actions and the `Action` union to keep dispatcher
# exhaustiveness honest (per §1.9.2 contract — no unused action variants).

@dataclass
class CascadeCloseLeaves:
    '''Cascade-close all named leaves under an epic in a single transaction.
    Used by `rule_cascade_close_on_merge` (§1.7) when the epic transitions to
    `lifecycle=merged` — leaves parked at `status=review_approved,
    lifecycle=holistic_review` close together with the epic. Idempotent:
    leaves already at `status=closed` are skipped without error.'''
    epic_task_id: str
    leaf_task_ids: list[str]

@dataclass
class MarkTaskReviewApproved:
    '''Synthetic dispatcher action that issues the `mark_task_review_approved`
    transition (§1.8) on behalf of the dispatcher. NOTE: no rule currently
    emits this action — the yolo cap-exhaustion fallbacks in `rule_qa`
    (§1.7) and §2.10 (merge yolo) use `AdvanceLifecycle(..., status=...)`
    instead, because routing through `mark_task_review_approved` would
    re-run the leaf-close gate (§1.8.6a) that just rejected the validation
    that triggered the cap exhaustion in the first place. This action is
    retained for forward compatibility: a future caller that intentionally
    wants the review gate to run (e.g. a dispatcher-side approval that
    follows a successful CI run) can route through here.'''
    task_id: str
    by_actor: str = "dispatcher"
    approval_notes: str = ""

@dataclass
class EmitStubManifest:
    """R7.F-yolo-manifest-fallback / R7.F-no-livelock: dispatcher-emitted
    deterministic stub manifest used by `rule_plan_adversary`'s yolo
    cap-exhausted fallback (§1.7). Wraps `gobby.plans.manifest_emitter.
    emit_stub_manifest(...)` from §2.21a — the dispatcher does NOT
    re-implement emission logic.

    Yolo-invariant contract: this action NEVER raises in the dispatch arm.
    The yolo cap-exhausted sequence MUST progress to `AdvanceLifecycle(test_arch)`
    deterministically — raising would leave the task at `plan_review` for
    the next tick, which would re-emit the audit marker and re-attempt the
    same failure (livelock). The §2.21a emitter absorbs every failure
    surface (missing artifact handled here, malformed plan handled in the
    emitter's `"fallback_force_approve"` path) and writes a `## Yolo
    Fallbacks` audit section explaining the outcome. Downstream
    `gobby expand` will reject the plan when it parses in
    `parse_mode="expansion"` if the manifest is genuinely unsalvageable —
    surfacing the issue at expansion time where a human can intervene
    rather than spinning the dispatcher.

    Failure-path contract (R7.F-no-livelock):

    1. **Missing `task_artifacts.plan_file_path`**: the dispatch arm
       appends a `## Yolo Fallbacks` audit marker via
       `append_description_section` to the planning anchor, recording
       "plan_file_path artifact missing on yolo cap exhaustion; force-
       advancing without manifest emission. Downstream gobby expand will
       reject this plan." Then it returns; the subsequent `AdvanceLifecycle`
       runs. The dispatch arm does NOT raise.

    2. **Existing manifest valid (`emit_stub_manifest` returns
       "noop_existing_valid")**: idempotent. No file change. Lifecycle
       advance proceeds.

    3. **Malformed existing manifest replaced ("replaced_malformed")**:
       emitter rewrote the manifest, post-write parse passed. Lifecycle
       advance proceeds.

    4. **Fresh emission ("fresh")**: emitter wrote a new manifest, post-
       write parse passed. Lifecycle advance proceeds.

    5. **Plan-shape unsalvageable ("fallback_force_approve")**: emitter
       appended its own `## Yolo Fallbacks` marker to the plan file
       documenting the issue. Dispatch arm proceeds to lifecycle advance
       — never raises. Downstream `gobby expand` rejection at expansion
       time is the documented escape hatch.
    """
    task_id: str
    by_actor: str = "dispatcher"

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
          | AdvanceLifecycle | CascadeCloseLeaves | EmitStubManifest
          | MarkTaskReviewApproved | ArchivePlan | EscalateTask
          | AppendAuditMarker | Skip)
```

`prompt_builder` is a string key resolved against a `PROMPT_BUILDERS: dict[str, Callable[[Task], tuple[str, dict]]]` registry. Each builder returns `(prompt, initial_variables)`. This keeps per-agent prompt construction out of the dispatcher core and addresses R2.F7 (prompts are not a single generic template).

**Acceptance:**

- 1.6.1 — Dispatch action wrappers is implemented according to this section. file: `src/gobby/dispatch/actions.py`.

### 1.6a Prompt-builder registry [category: code] (depends: 1.6)

`kind: deliverable`

Target: `src/gobby/dispatch/prompts.py` (new file).

R7.F-prompt-builders fix. The dispatcher's `_dispatch(SpawnAgent)` arm (§1.9) does `prompt, initial_vars = PROMPT_BUILDERS[s.prompt_builder](task)`. Every spawn rule in §1.7 emits a `prompt_builder` string key (`planner_rewrite`, `plan_adversary`, `test_architect`, `expansion_qa`, `developer`, `qa_reviewer`, `holistic_reviewer`, `merge_runner`). Without the registry module, the dispatcher's import fails and the first spawn path raises `KeyError`. This deliverable creates the registry alongside §1.6's actions module so §1.7 + §1.9 can land coherently.

**Module shape**:

```python
# src/gobby/dispatch/prompts.py
from collections.abc import Callable
from gobby.storage.tasks._models import Task

PromptBuilder = Callable[[Task], tuple[str, dict]]

# One builder per spawn key emitted by §1.7's rules. Each builder reads
# durable task state (description, labels, task_artifacts, parent epic) and
# returns (prompt_text, initial_variables_dict). Builders are pure: no MCP
# calls, no side effects. The dispatcher's mutex hold makes the read
# consistent with the locked re-evaluation.
def _build_planner_rewrite(task: Task) -> tuple[str, dict]: ...
def _build_plan_adversary(task: Task) -> tuple[str, dict]: ...
def _build_test_architect(task: Task) -> tuple[str, dict]: ...
def _build_expansion_qa(task: Task) -> tuple[str, dict]: ...
def _build_developer(task: Task) -> tuple[str, dict]: ...
def _build_qa_reviewer(task: Task) -> tuple[str, dict]: ...
def _build_holistic_reviewer(task: Task) -> tuple[str, dict]: ...
def _build_merge_runner(task: Task) -> tuple[str, dict]: ...

PROMPT_BUILDERS: dict[str, PromptBuilder] = {
    "planner_rewrite":     _build_planner_rewrite,
    "plan_adversary":      _build_plan_adversary,
    "test_architect":      _build_test_architect,
    "expansion_qa":        _build_expansion_qa,
    "developer":           _build_developer,
    "qa_reviewer":         _build_qa_reviewer,
    "holistic_reviewer":   _build_holistic_reviewer,
    "merge_runner":        _build_merge_runner,
}
```

The 8-key roster is the closed set the dispatcher needs as of this epic; adding a new spawn rule MUST also add a builder key here, and `tests/dispatch/test_prompt_builders.py` enforces the bijection between `RULES`-emitted keys and `PROMPT_BUILDERS` keys.

**Acceptance:**

- 1.6a.1 — `src/gobby/dispatch/prompts.py` exposes `PROMPT_BUILDERS` with all eight keys above and a `PromptBuilder` type alias. file: `src/gobby/dispatch/prompts.py`. test: `tests/dispatch/test_prompt_builders.py::test_registry_keys_present` covers each key.
- 1.6a.2 — Bijection invariant: every `prompt_builder` string emitted by `RULES` (§1.7) resolves to a key in `PROMPT_BUILDERS`; no orphan keys exist on either side. test: `tests/dispatch/test_prompt_builders.py::test_rules_keys_match_registry` introspects `RULES` for all `SpawnAgent.prompt_builder` literals and asserts the set equals `PROMPT_BUILDERS.keys()`. Adding a new rule without a builder fails this test.
- 1.6a.3 — Each builder returns `(str, dict)` and is pure (no MCP/DB side effects beyond reading the passed `Task`). Each builder's prompt names the specific contract surface the spawned agent reads (e.g., `qa_reviewer` references the `qa-reviewer.yaml` persona contract from §2.11). test: `tests/dispatch/test_prompt_builders.py::test_each_builder_signature_and_purity` covers each builder under a mocked `Task` fixture.

### 1.7 Decision rules for all stages [category: code] (depends: 1.6, 1.6a)

`kind: deliverable`

Target: `src/gobby/dispatch/rules.py` (new file)

Ordered, first-match-wins. No `STAGE_BY_PROFILE` map — `_stage_enabled(task, stage)` is simply "stage is not in `stage-:*` labels." Profile resolution happens at the CLI layer (§3.2), which writes the skip-stage labels. The rule engine reads resolved state only.

**Yolo-never-escalates**: every rule that would return `EscalateTask` inspects `_is_yolo(task)` first. If yolo, it returns a deterministic fallback action (documented in the per-rule yolo-fallback column below) plus an `AppendAuditMarker` so the fallback is visible in the task's description history. Since the dispatcher already runs multiple rules per tick via re-evaluation under the mutex (§1.9), returning a tuple of actions is allowed — the dispatcher executes them in order.

```python
from gobby.dispatch.actions import (
    Action, AdvanceLifecycle, AppendAuditMarker, ArchivePlan,
    CascadeCloseLeaves, CreateClone, CreateWorktree, EmitStubManifest,
    EscalateTask, Skip, SpawnAgent, StartExpansionRun,
)
# Note: `MarkTaskReviewApproved` is defined in actions but no rule emits it —
# the yolo cap-exhaustion paths use `AdvanceLifecycle(..., status=...)`
# directly (per §1.8.6 which would otherwise re-run the failing review gate).
# The dispatcher (§1.9) keeps the match arm for forward compatibility but
# its trigger documentation explicitly says "no rule currently emits this".
from gobby.storage.tasks._crud import _is_yolo, _skipped_stages
from gobby.storage.tasks._models import Isolation, Lifecycle, Task

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
        # R7.F-bypass-manifest fix: stage-skip is allowed only when an
        # existing `## M1 Task Manifest` is already present in the plan
        # file (verified at `gobby build` time per §3.2.7 — the build
        # service rejects `--skip-stage plan_review` for plan-file/epic
        # builds whose plan does not parse cleanly under
        # `parse_plan(plan_path, parse_mode="expansion")`). By the time the
        # dispatcher sees the task, the manifest invariant already holds,
        # so the skip path is safe to advance directly.
        if task.lifecycle == Lifecycle.plan_review:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.test_arch,
                                    reason="plan_review stage skipped (manifest precondition validated at build time)",
                                    by_actor="dispatcher")
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
            # R7.F-yolo-manifest-fallback: yolo cap exhaustion preserves the
            # "yolo never escalates" invariant AND the §2.21 manifest-presence
            # invariant (downstream `gobby expand` will read the plan in
            # `parse_mode="expansion"`, which requires a manifest). The dispatcher
            # cannot run plan-adversary's manifest-emission step itself (the
            # adversary just exhausted its rounds without approving), so it emits
            # a deterministic stub manifest from the plan's `kind: deliverable`
            # sections via `EmitStubManifest` — one entry per deliverable, default
            # `assigned_agent="backend-developer"`, `tdd=True` for code/test
            # categories, `source_section` set to each deliverable's section ID,
            # and a `covers:<plan-id>:<section-id>:<item-id>` label per acceptance
            # item. The stub is a concession to the yolo invariant; an audit
            # marker captures the override so a human reviewing the merged epic
            # can see that the manifest was force-generated rather than authored.
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                                  body=f"{_now_iso()}: plan_review round budget exhausted; "
                                       f"manifest auto-generated from kind:deliverable sections; "
                                       f"plan force-approved."),
                EmitStubManifest(task_id=task.id, by_actor="dispatcher"),
                AdvanceLifecycle(task_id=task.id, to=Lifecycle.test_arch,
                                 reason="yolo force-approve (rounds exhausted; stub manifest emitted)",
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

# Default retry caps. Used as fallbacks when `task_artifacts` does not carry a
# resolved override (fresh install, ad-hoc dispatch, or pre-§2.19 builds). Rule
# code MUST read via `_resolve_retry_cap(task, name, default)` so per-build
# overrides written into `task_artifacts` at `gobby build` time (§2.19) take
# effect without rule-code edits. Naming the defaults explicitly (`_DEFAULT`
# suffix) makes the read path unambiguous and prevents a future refactor from
# accidentally reverting to direct constant reads.
MAX_EXPANSION_ATTEMPTS_DEFAULT = 3

def _resolve_retry_cap(task: Task, name: str, default: int) -> int:
    """Resolve a retry cap for `task` by name. Reads `task_artifacts` first
    (the resolved value persisted by `gobby build` at dispatch time, §2.19);
    falls back to the module-level `_DEFAULT` constant if absent. The helper
    is the single read path so adding a new cap is a one-line change here +
    one acceptance item in §2.19."""
    artifacts_value = _retry_cap_from_artifacts(task, name)
    return artifacts_value if artifacts_value is not None else default

def rule_start_expansion(task: Task) -> Action | None:
    """R4.F1 — first half of the split. Starts a new expansion run ONLY when
    `task_artifacts.expansion_run_id` is missing. R7.F-start-vs-validate fix:
    the prior `not _expansion_active(task)` predicate matched on terminal
    (`completed`/`failed`) runs too — letting the start rule overwrite the
    terminal run before `rule_validate_expansion` could dispatch expansion-qa
    to read failure details and reject. Guarding on a MISSING artifact
    instead of an INACTIVE run preserves the recovery path: terminal runs
    keep their `expansion_run_id` until expansion-qa rejects (which §2.9
    clears the artifact and increments `expansion_attempts`), THEN this rule
    re-fires on the next tick.

    Cap retries; non-yolo escalates, yolo force-advances."""
    if not _stage_enabled(task, "expanding"):
        if task.lifecycle == Lifecycle.expanding:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.in_development,
                                    reason="expanding stage skipped", by_actor="dispatcher")
        return None
    if not (task.lifecycle == Lifecycle.expanding
            and task.status == "open"
            and _expansion_run_artifact_absent(task)):  # was: not _expansion_active(task)
        return None
    attempts = _expansion_attempts(task)
    cap = _resolve_retry_cap(task, "max_expansion_attempts", MAX_EXPANSION_ATTEMPTS_DEFAULT)
    if attempts >= cap:
        if _is_yolo(task):
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                    body=f"{_now_iso()}: expansion attempts exhausted ({attempts}/{cap}); force-advanced to in_development."),
                AdvanceLifecycle(task_id=task.id, to=Lifecycle.in_development,
                                 reason="yolo: expansion attempts exhausted",
                                 by_actor="dispatcher"),
            ]
        return EscalateTask(task_id=task.id,
            reason=f"needs_human: expansion failed {attempts} times (cap={cap}); review rejection notes and either fix the plan or de-escalate to retry")
    return StartExpansionRun(task_id=task.id)

def rule_validate_expansion(task: Task) -> Action | None:
    """R4.F1 — second half of the split. When the expansion run reaches a
    terminal state (`completed` OR `failed`) and validation has not yet been
    dispatched, spawn `expansion-qa`. Expansion-qa reads the run details and
    routes: completed → validate the produced tree; failed → call
    `mark_task_review_rejected(lifecycle=expanding)` with the failure reason
    in `rejection_notes`, which clears `expansion_run_id` and increments
    `expansion_attempts` (§1.8 R4.F1 extension), letting `rule_start_expansion`
    re-fire on the next tick. R7.F-failed-run fix: covering `failed` in this
    predicate is what makes recovery actually work — the prior `_completed`-only
    predicate stranded failed runs because nothing cleared their artifact.

    The candidate scan + per-task mutex prevent duplicate dispatch within a tick;
    expansion-qa's claim flips `claimed_by_session_id` for the lifetime of the
    run, so the candidate scan filters this task out across ticks."""
    if not _stage_enabled(task, "expanding"):
        return None
    if (task.lifecycle == Lifecycle.expanding
        and task.status == "open"
        and _expansion_run_terminal(task)):
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

MAX_QA_ROUNDS_DEFAULT = 5
MAX_MERGE_ATTEMPTS_DEFAULT = 3      # R7.F-merge-cap-wired: read by `rule_merging` and threaded into merge.yaml session vars
MAX_HOLISTIC_ROUNDS_DEFAULT = 3     # R7.F-holistic-cap-wired: read by `rule_holistic` for cap exhaustion
MAX_REVIEW_ROUNDS_DEFAULT = 3       # R7.F-review-cap-wired: read by `_rounds_remaining` for plan-review budget

def rule_qa(task: Task) -> Action | None:
    '''Per-leaf QA dispatch with retry cap. The agent dispatched is the
    read-only `qa-reviewer` (§2.11) — Claude operating with a ruthless
    senior-dev persona. Approval flips the leaf to
    `status=review_approved, lifecycle=holistic_review` (§1.8); rejection
    bumps `qa-attempts:N` and reopens the leaf for another dev pass. On cap
    exhaustion, non-yolo escalates with `needs_human:` reason; yolo emits
    `[AppendAuditMarker, AdvanceLifecycle(holistic_review, status="review_approved")]`
    — the explicit-transition action bypasses the leaf-close gate that would
    otherwise re-run the failing validation that triggered the cap exhaustion
    (per §1.8.6a). The audit marker is the durable record of the override.
    The cap is resolved via `_resolve_retry_cap` so `gobby build --max-qa-rounds N`
    overrides flow through (§2.19).'''
    if not _stage_enabled(task, "qa"):
        return None
    if not (task.lifecycle == Lifecycle.in_development
            and task.status == "needs_review"
            and not task.claimed_by_session_id):
        return None
    attempts = _qa_attempts(task)
    cap = _resolve_retry_cap(task, "max_qa_rounds", MAX_QA_ROUNDS_DEFAULT)
    if attempts >= cap:
        if _is_yolo(task):
            # R7.F-yolo-fallback-gate: the force-advance path bypasses qa
            # validation by design (yolo: never escalate, push work through),
            # so it MUST NOT route through `MarkTaskReviewApproved` whose
            # leaf-close gate would re-evaluate the failing validation. Use
            # the explicit `AdvanceLifecycle` action + audit marker. The leaf
            # parks at `(holistic_review, review_approved)` exactly as the
            # normal-approval path does (advance_lifecycle resets status to
            # `review_approved` for this lifecycle target — see §1.8). The
            # audit marker is the durable record that the override happened.
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                    body=f"{_now_iso()}: qa rounds exhausted ({attempts}/{cap}); "
                         f"leaf force-advanced to holistic_review without qa approval."),
                AdvanceLifecycle(task_id=task.id, to=Lifecycle.holistic_review,
                                 reason=f"yolo: qa rounds exhausted after {attempts} attempts (cap={cap})",
                                 by_actor="dispatcher",
                                 status="review_approved"),  # explicit terminal status — bypasses leaf-close gate
            ]
        return EscalateTask(task_id=task.id,
            reason=f"needs_human: qa-reviewer rejected leaf {attempts} times "
                   f"(cap={cap}); review the rejection notes and either fix "
                   f"the leaf or de-escalate with override")
    return SpawnAgent(
        agent="qa-reviewer", task_id=task.id, prompt_builder="qa_reviewer",
        additional_skills=task.additional_skills,
    )

def rule_all_leaves_holistic(task: Task) -> Action | None:
    '''Renamed from `rule_all_closed_advance_to_holistic`. Fires when every
    child leaf is in a terminal-or-audit-pending state — either
    `status=closed` or `(status=review_approved AND lifecycle=holistic_review)`.
    If at least one leaf is audit-pending, advance the epic to `holistic_review`
    (or the next enabled stage) so holistic-reviewer audits the cumulative diff.
    Trivial-epic shortcut: if every leaf is `closed` (no audit-pending leaves),
    holistic skips and the epic respects the `stage-:pr` label only — `merging`
    is non-skippable per Constraints, so the chain always lands at `merging`
    and runs the merge agent.'''
    if not (task.task_type == "epic"
            and task.lifecycle == Lifecycle.in_development
            and _all_leaves_terminal_or_holistic(task)):
        return None
    if _any_leaves_holistic_pending(task):
        target = (Lifecycle.holistic_review if _stage_enabled(task, "holistic_review")
                  else Lifecycle.pr if _stage_enabled(task, "pr")
                  else Lifecycle.merging)  # `merging` is non-skippable; always reachable
        return AdvanceLifecycle(task_id=task.id, to=target,
            reason="all leaves terminal-or-holistic; audit pending",
            by_actor="dispatcher")
    # Trivial-epic shortcut: every leaf closed cleanly. Skip holistic.
    target = (Lifecycle.pr if _stage_enabled(task, "pr")
              else Lifecycle.merging)  # `merging` is non-skippable; always reachable
    return AdvanceLifecycle(task_id=task.id, to=target,
        reason="all subtasks closed (trivial epic; holistic skipped)",
        by_actor="dispatcher")

def rule_holistic(task: Task) -> Action | None:
    """R7.F-holistic-cap-wired: holistic-review rejection cap. After a
    cited-subtasks rejection (§1.8 R4.F5), the epic rewinds to in_development
    and the leaves redo dev/qa. When all leaves return to terminal-or-holistic,
    `rule_all_leaves_holistic` fires and advances back to holistic_review.
    Each holistic-reviewer rejection bumps a `holistic-attempts:N` label on
    the epic (counter symmetric to `qa-attempts:N` on leaves). At cap
    exhaustion, non-yolo escalates with `needs_human:` reason; yolo emits the
    deterministic force-advance fallback per §2.10's pattern."""
    if not _stage_enabled(task, "holistic_review"):
        if task.lifecycle == Lifecycle.holistic_review:
            return AdvanceLifecycle(task_id=task.id, to=Lifecycle.pr,
                                    reason="holistic_review stage skipped", by_actor="dispatcher")
        return None
    if not (task.task_type == "epic"
            and task.lifecycle == Lifecycle.holistic_review
            and task.status in ("open", "needs_review")
            and not task.claimed_by_session_id):
        return None
    attempts = _holistic_attempts(task)
    cap = _resolve_retry_cap(task, "max_holistic_rounds", MAX_HOLISTIC_ROUNDS_DEFAULT)
    if attempts >= cap:
        if _is_yolo(task):
            # Yolo never escalates: force-advance to pr (or merging if pr is
            # skipped) via explicit advance_lifecycle (bypasses the
            # stage-advance gate that would re-evaluate holistic preconditions).
            target = (Lifecycle.pr if _stage_enabled(task, "pr") else Lifecycle.merging)
            return [
                AppendAuditMarker(task_id=task.id, heading="Yolo Fallbacks",
                    body=f"{_now_iso()}: holistic_review rounds exhausted ({attempts}/{cap}); "
                         f"epic force-advanced to {target.value} without holistic approval."),
                AdvanceLifecycle(task_id=task.id, to=target,
                                 reason=f"yolo: holistic rounds exhausted after {attempts} attempts (cap={cap})",
                                 by_actor="dispatcher"),
            ]
        return EscalateTask(task_id=task.id,
            reason=f"needs_human: holistic-reviewer rejected epic {attempts} times "
                   f"(cap={cap}); review the rejection notes and either fix the "
                   f"cited subtasks or de-escalate with override")
    return SpawnAgent(
        agent="holistic-reviewer", task_id=task.id, prompt_builder="holistic_reviewer",
        # R7.F-holistic-cap-wired: pass resolved cap so holistic-reviewer
        # can include it in approval/rejection signoff_summary context.
        initial_variables={"max_holistic_rounds": cap, "current_attempt": attempts + 1},
    )

def rule_pr(task: Task) -> Action | None:
    """R3.F4 + yolo fallback. No PR-creation agent exists until #12728.
    Non-yolo: escalate; human opens PR manually and calls
    `de_escalate_task(task_id, target_status="review_approved", lifecycle=Lifecycle.merging)`
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
                   "target_status='review_approved', lifecycle=Lifecycle.merging).",
        )
    return None

def rule_merging(task: Task) -> Action | None:
    # `merging` is non-skippable per Constraints. There is no `_stage_enabled`
    # branch here — a malformed `stage-:merging` label cannot bypass the merge
    # agent. `gobby build` rejects `--skip-stage merging` at build time (§3.2);
    # if a `stage-:merging` label is observed at dispatch time despite that
    # validation, an audit marker is written and the rule still dispatches the
    # merge agent (the label is observed-but-ignored on this stage).
    if (task.task_type == "epic"
        and task.lifecycle == Lifecycle.merging
        and task.status in ("open", "review_approved")
        and not task.claimed_by_session_id):
        merge_cap = _resolve_retry_cap(task, "max_merge_attempts", MAX_MERGE_ATTEMPTS_DEFAULT)
        merge_vars = {
            "yolo": _is_yolo(task),
            "max_merge_attempts": merge_cap,  # R7.F-merge-cap-wired: merge.yaml reads this from session vars per §2.10
        }
        if "stage-:merging" in task.labels:
            return [
                AppendAuditMarker(task_id=task.id, heading="Stage-Skip Audit",
                    body=f"{_now_iso()}: stage-:merging label present but ignored — merging is non-skippable; dispatching merge agent."),
                SpawnAgent(agent="merge", task_id=task.id, prompt_builder="merge_runner",
                    initial_variables=merge_vars),
            ]
        return SpawnAgent(
            agent="merge", task_id=task.id, prompt_builder="merge_runner",
            initial_variables=merge_vars,   # agent's conflict handling branches on yolo + reads cap
        )
    return None

def rule_cascade_close_on_merge(task: Task) -> Action | None:
    '''When an epic transitions to `lifecycle=merged`, every leaf parked at
    `status=review_approved, lifecycle=holistic_review` cascades to
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

Helpers `_current_verdict_rejected`, `_rounds_remaining` (R7.F-review-cap-wired: reads `_resolve_retry_cap(task, "max_review_rounds", MAX_REVIEW_ROUNDS_DEFAULT)` for the cap and `planning-round:N` label for the live counter; returns `cap > current_round`), `_expansion_run_artifact_absent`, `_expansion_run_terminal`, `_expansion_attempts`, `_qa_attempts`, `_retry_cap_from_artifacts`, `_target_branch`, `_is_coding_epic`, `_has_ready_subtasks`, `_all_leaves_terminal_or_holistic`, `_any_leaves_holistic_pending`, `_leaves_pending_cascade_close`, `_has_plan_file`, `_plan_already_archived`, `_plan_id_for`, `_skipped_stages`, `_is_yolo`, `_has_worktree`, `_has_clone`, `_has_isolation_artifact`, `_parent_epic`, `_now_iso` live alongside rules. `_retry_cap_from_artifacts(task, name) -> int | None` reads the cap key from `task_artifacts` (set by `gobby build` per §2.19) and returns `None` when absent; `_resolve_retry_cap(task, name, default)` (defined inline above) wraps it with the `_DEFAULT` fallback. `_qa_attempts(task)` reads the leaf's `qa-attempts:N` label (set by `mark_task_review_rejected` at `lifecycle=in_development`, §1.8). `_holistic_attempts(task)` reads the epic's `holistic-attempts:N` label (set by `mark_task_review_rejected` at `lifecycle=holistic_review`, R7.F-holistic-cap-wired — symmetric to qa-attempts on leaves; §1.8's holistic_review rejection branch increments it before reopening cited subtasks). `_all_leaves_terminal_or_holistic(epic)` returns true when every leaf is `status=closed` or `(status=review_approved AND lifecycle=holistic_review)`; `_any_leaves_holistic_pending(epic)` returns true when at least one leaf is in the holistic-pending state (drives the trivial-epic shortcut in `rule_all_leaves_holistic`). `_leaves_pending_cascade_close(epic)` returns the list of leaf IDs at `status=review_approved, lifecycle=holistic_review` that the cascade-close action must close. `_has_plan_file(epic)` checks `task_artifacts.plan_file_path` non-null; `_plan_already_archived(epic)` checks the `plans` table (§2.15) for `state=archived`; `_plan_id_for(epic)` derives the `plan_id` from `task_artifacts.plan_file_path` or the `plans` table row. `_has_isolation_artifact(epic)` returns true when the appropriate artifact column for the epic's `isolation` is populated (`worktree_path` for `worktree`, `clone_path` for `clone`); for `isolation=none` it returns true unconditionally (in-branch work needs no artifact). They read durable task state: labels (`planning-current-verdict:rejected`, `planning-round:N`, `planning-max-rounds:N`, `qa-attempts:N`, `stage-:<name>`), `task_artifacts` rows (R4.F1 expansion fields, R4.F6 target_branch, plan_file_path), the expansion-run table (for `_expansion_run_completed`), the `plans` table (§2.15) for archive state, subtask tree presence, and task fields on `tasks`. `_target_branch` falls back to `git rev-parse --abbrev-ref HEAD` when `task_artifacts.target_branch` is absent (legacy tasks created before R4.F6). **No `_get_stack`, no `_get_profile`, no `_added_stages`, no `_expansion_started`, no `_all_subtasks_closed`** — those helpers are obsolete:

- `stack` was replaced by `assigned_agent` (§2.8).
- Profile is CLI sugar only; no label storage, no helper needed.
- `stage+:` is gone; the full pipeline is the default, `stage-:` removes stages.

**Acceptance:**

- 1.7.1 — Decision rules for all stages is implemented according to this section. file: `src/gobby/dispatch/rules.py`.
- 1.7.2 — Retry caps resolve through `_resolve_retry_cap(task, name, default)`, which reads `task_artifacts` (set by `gobby build` per §2.19) and falls back to module-level defaults. Defaults: `MAX_EXPANSION_ATTEMPTS_DEFAULT = 3`, `MAX_QA_ROUNDS_DEFAULT = 5`. `rule_start_expansion` reads `max_expansion_attempts`; `rule_qa` reads `max_qa_rounds`. On cap exhaustion non-yolo returns `EscalateTask` with `needs_human:` reason citing the resolved cap. Yolo returns `[AppendAuditMarker, AdvanceLifecycle]` for both rules (`AdvanceLifecycle` not `MarkTaskReviewApproved`, because the leaf-close gate at `lifecycle=in_development` would re-evaluate the failing validation that triggered the cap-exhaustion in the first place — yolo cap-exhaustion is the explicit force-advance escape hatch, so it must use the explicit transition tool that bypasses review-style gates). The leaf advances to `(holistic_review, review_approved)` via `advance_lifecycle(task, to=holistic_review, status="review_approved", ...)` per §1.8. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_qa_cap.py` covers (a) cap exhaustion non-yolo escalates, (b) cap exhaustion yolo issues `AdvanceLifecycle(holistic_review, status="review_approved")` + audit marker (NOT `MarkTaskReviewApproved` — explicit assertion that no review-gate evaluation happens on this path), (c) under-cap dispatches qa-reviewer agent, (d) cap override from `task_artifacts` takes precedence over the default; `tests/dispatch/test_rules_expansion_cap.py` covers the same shape for expansion attempts.
- 1.7.3 — `rule_all_leaves_holistic` (renamed from `rule_all_closed_advance_to_holistic`) fires per the mixed-state predicate: every child is `status=closed` OR `(status=review_approved AND lifecycle=holistic_review)`. When at least one child is audit-pending, advance to `holistic_review` (or next enabled stage); when every child is closed (no audit pending), apply the trivial-epic shortcut and skip holistic. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_holistic.py` covers (a) mixed-state with audit-pending → holistic_review, (b) all-closed → skip holistic, (c) mid-state non-firing.
- 1.7.4 — `rule_cascade_close_on_merge` returns `CascadeCloseLeaves(epic_task_id, leaf_task_ids)` when the epic has `lifecycle=merged` and `_leaves_pending_cascade_close(task)` returns a non-empty list. Idempotent — already-closed leaves are excluded by the helper. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_cascade_close.py` covers (a) cascade fires for audit-pending leaves, (b) all-already-closed → no-op, (c) leaf-list correctness.
- 1.7.5 — `rule_archive_plan_on_merge` returns `ArchivePlan(epic_task_id, plan_id)` when the epic has `lifecycle=merged, status=closed`, has `task_artifacts.plan_file_path`, and the plan is not already archived in the `plans` table (§2.15). file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_archive_plan.py` covers (a) fires on terminal close with active plan, (b) no-op when plan already archived, (c) no-op when no plan_file_path artifact.
- 1.7.6 — RULES tuple ordering: `rule_qa` precedes `rule_all_leaves_holistic`; `rule_cascade_close_on_merge` and `rule_archive_plan_on_merge` are appended after `rule_merging`. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_ordering.py`.

### 1.8 Lifecycle transitions in review tools [category: code]

`kind: deliverable`

Target: `src/gobby/storage/tasks/_transitions.py` and the matching MCP wrappers

**Core contract**: when `mark_task_review_approved` triggers a lifecycle advance, it also **resets `status` to `open`** so the next stage's rule can dispatch (rules gate on `status in ("open", "needs_review")`). The approval event is preserved in `task_lifecycle_events` (§1.1c) — audit trail is not lost.

Extend existing transitions and add one new one:

- **`mark_task_review_approved`**:
  - `lifecycle=plan_review` → advance to `test_arch`; status resets to `open`.
  - `lifecycle=test_arch` → advance to `expanding`; status resets to `open`.
  - `lifecycle=expanding` → advance to `in_development`; status resets to `open`. (Called by expansion-qa after successful validation — §2.9 wires this.)
  - `lifecycle=holistic_review` (epic) → **advance to `pr`** (R3.F4 fix); status resets to `open`. If `pr` is skipped via `stage-:pr`, further advance to `merging` — but NEVER directly to `merged`. `merging` is non-skippable per Constraints; the merge agent always runs. A `stage-:merging` label is observed-but-ignored at this transition (and §3.2 rejects it at build time).
  - `lifecycle=merging` → advance to `merged` (terminal); status = `closed`.
  - `lifecycle=in_development` (leaf — qa-reviewer caller) → advance the leaf to `lifecycle=holistic_review` and set `status=review_approved`. Leaf does **not** close — it parks at `holistic_review + review_approved` until the parent epic merges, at which point `rule_cascade_close_on_merge` (§1.7) cascades the leaf to `status=closed`. The atomic leaf transition is what trips `rule_all_leaves_holistic` (§1.7) once every sibling reaches a terminal-or-holistic state. The transition writes a `task_lifecycle_events` row with `reason="mark_task_review_approved"` and `by_actor=<current_session_agent_name>` (typically `qa-reviewer` or `dispatcher` for yolo fallbacks).
  - Every advance writes a `task_lifecycle_events` row with `reason="mark_task_review_approved"` and `by_actor=<current_session_agent_name>`.

- **`mark_task_review_rejected(task_id, rejection_notes=None, round_number=None, cited_subtasks=None)`**: extended signature — `cited_subtasks` is a list of leaf refs (R4.F5). Behavior by lifecycle:
  - `lifecycle=plan_review` → stays at `plan_review`; adds `planning-current-verdict:rejected` label; increments `planning-round:N`; appends findings to description (existing behavior per R2.F1). `cited_subtasks` ignored.
  - `lifecycle=holistic_review` (R4.F5 fix; epic only) → `cited_subtasks` is **REQUIRED** (one or more leaf refs needing rework); rejection without it raises a validation error. The tool atomically (single transaction): (a) appends findings to the epic description, (b) reopens each cited subtask (`status: review_approved | closed → open`; `lifecycle: holistic_review → in_development` for leaves rewound from the audit-pending park), (c) rewinds the epic lifecycle `holistic_review → in_development` with `status=open`, and (d) **increments `holistic-attempts:N` label** on the epic (R7.F-holistic-cap-wired — symmetric to `qa-attempts:N` on leaves; consumed by `rule_holistic`'s cap-exhaustion branch in §1.7). The atomic reopen prevents `rule_all_leaves_holistic` (§1.7, renamed) from immediately bouncing the epic back into `holistic_review` on the next tick (because at least one subtask is now `open` at `in_development`, the predicate is false until the dev/qa loop drives the cited leaves back to terminal-or-holistic). The escalate-rescope third path (below) is the only no-cited rejection mechanism — bare rejection without `cited_subtasks` is invalid by design.
  - `lifecycle=expanding` (R4.F1 extension) → stays at `expanding`; findings appended; **clears `task_artifacts.expansion_run_id`** (so `rule_start_expansion` can re-fire on the next tick); **calls `increment_expansion_attempts(task_id)`** so the retry cap is enforced. §2.9 (expansion-qa) is the caller.
  - `lifecycle=merging` (R6.F4 extension) → stays at `merging`; status resets to `open`; findings appended (yolo retry detail or non-yolo failure note); **does NOT itself manipulate the `merge-attempts:N` label** — that label is managed by the merge agent (§2.10) immediately before the rejection call. The tool atomically (single transaction) appends findings, resets status, and writes the rejection event to `task_lifecycle_events`. `rule_merging` re-dispatches the merge agent on the next tick. §2.10 (merge agent yolo retry path) is the caller. After `merge-attempts:N >= cap`, the merge agent switches to the force-advance fallback: `gobby-tasks-ops:append_description_section(heading="Yolo Fallbacks", body=...)` + `gobby-tasks:advance_lifecycle(epic, to=Lifecycle.merged, reason="yolo: merge attempts exhausted; force-advanced without merge", by_actor="merge")` — `advance_lifecycle`, not `mark_task_review_approved`, because the stage-advance gate at `lifecycle=merging` (§1.8.6b) requires merge-clean state and would deadlock the fallback. §2.10 documents the full sequence.
  - Leaf `lifecycle=in_development` (qa-reviewer caller) → no lifecycle change; `status: needs_review → open`; rejection_notes appended to description; **increments `qa-attempts:N` label** (matching the cap-enforcement contract in `rule_qa`, §1.7 — `MAX_QA_ROUNDS=5`). The leaf re-enters the dev/qa loop on the next tick. `cited_subtasks` ignored. After the cap, behavior is rule-driven (escalate non-yolo / force-advance yolo per `rule_qa` cap exhaustion).

- **Holistic-review escalate-rescope (third path)**: when the holistic-reviewer determines the plan premise is wrong rather than the implementation, it calls `escalate_task(task_id=epic, reason="needs_human:rescope_required:<details>")` (or `reason="needs_human:requirements_unclear:<details>"`). The tool flips the epic to `status=escalated`, leaves `lifecycle=holistic_review` unchanged, and writes a `task_lifecycle_events` row. The user resumes via the existing extended `de_escalate_task(epic, target_status=..., lifecycle=..., reason=...)` after revising the plan or accepting the rework scope. Validation: `escalate_task` rejects reasons that don't start with `needs_human:rescope_required:` or `needs_human:requirements_unclear:` when the caller is the holistic-reviewer agent (enforced via `by_actor` check); other agents may use other `needs_human:` prefixes per existing escalation conventions.

- **`mark_task_needs_review`**: no lifecycle change. The transition tool itself clears the `planning-current-verdict:rejected` label atomically with the status change to `needs_review` (R2.F1 + R7.F-planner-resubmit). The planner agent's terminal step calls `mark_task_needs_review(task_id, review_notes=...)` — defined as a §2.23.5 deliverable — and the tool's atomic clear-and-set guarantees `rule_plan_rewrite_on_reject` (§1.7) cannot re-fire on the same task in any intermediate state.

- **New tool `advance_lifecycle(task_id, to, reason, by_actor, status=None)`**: MCP-exposed explicit transition. `reason` is **mandatory** (TEXT NOT NULL in `task_lifecycle_events`); `by_actor` defaults to the calling session's agent name. Writes the lifecycle event row and updates `tasks.lifecycle`. The `status` parameter is **optional** and explicit:
  - When `status` is omitted, the tool resolves a default based on `(to, task_type)`: `merged` → `closed`; leaf advancing to `holistic_review` (yolo qa-fallback parking) → `review_approved`; everything else → `open`.
  - When `status` is provided, the tool uses that value verbatim. Used by yolo merge-cap-exhaustion (`status=closed` alongside `to=merged`) and yolo qa-cap-exhaustion (`status=review_approved` alongside `to=holistic_review`) — both bypass review gates by design and assert the desired terminal status explicitly.
  - The tool does NOT run review-style validation gates (it is the explicit-transition escape hatch). The session-context rules still apply (autonomous-only, etc.).

- **Extended tool `de_escalate_task(task_id, target_status, lifecycle=None, reason=None)`**: now accepts an optional `lifecycle` parameter for single-call recovery. Matters for pr-escalation and holistic-rescope: after a human opens a PR, they run `de_escalate_task(task_id, target_status="review_approved", lifecycle=Lifecycle.merging, reason="human opened PR #N")`; after a rescope-escalation, they revise the plan and run `de_escalate_task(epic, target_status="open", lifecycle=Lifecycle.in_development, reason="rescoped per revised plan")`. The tool:
  1. Clears the escalated state, sets `status = target_status`.
  2. If `lifecycle` is provided, also calls `advance_lifecycle(task_id, to=lifecycle, reason=reason or "de-escalation", by_actor="human")`.
  3. Writes the combined change in a single transaction.

Session-context enforcement stays as today (mark_* autonomous-only; close_task interactive unless escaping with labels). §2.14a extends this to scope interactive-only hooks (e.g., `require-task-close`) so they do not block autonomous build agents calling `mark_task_*`.

**Validation gates are verdict-AND-stage-aware** (resolves both the read-only-reviewer trap and the non-code-stage-approval trap):

The single helper `_run_validation_gates(task, verdict, lifecycle)` switches on the `(verdict, lifecycle)` tuple. Three gate sets exist:

| Gate set | When it fires | Required gates |
|----------|---------------|----------------|
| **leaf-close** | `verdict=approved` AND the calling transition is the leaf-approval path (`mark_task_review_approved` on a leaf at `lifecycle=in_development` — qa-reviewer caller). This is the only verdict path that ships code; the leaf parks at `(holistic_review, review_approved)` until cascade-close, but the work is done. | `commit-attached`, `validation_criteria` pass, `errors_resolved`, `memory_review_completed`. No `skip_validation`. |
| **stage-advance** | `verdict=approved` AND the calling transition is a stage-advancement path (plan-adversary approving an anchor or epic at `lifecycle=plan_review`; test-architect at `lifecycle=test_arch`; expansion-qa at `lifecycle=expanding`; holistic-reviewer approving an epic at `lifecycle=holistic_review`; merge agent finalizing at `lifecycle=merging → merged`). These approvals advance lifecycle; they do NOT necessarily ship code on the task being approved. Stage-specific outputs gate the approval instead. | Stage-specific. plan_review: adversary's `## Task Manifest` was emitted on the plan file (or — when the anchor is the verdict target rather than the planning epic — `signoff_summary` was provided). test_arch: test architecture description was appended. expanding: an expansion run completed and the validated tree exists. holistic_review: every leaf is in a terminal-or-holistic state. merging: a merge commit / clean state on the target branch. None of these gates require `commit-attached` or `validation_criteria` on the approving task. |
| **light** | `verdict=rejected` OR `verdict=escalated` on any task at any lifecycle. Rejection IS the path for reporting failed validation; requiring it to pass would deadlock. | Valid claim/session for the calling agent, and either `rejection_notes` or `reason` non-empty. No commit, no validation_criteria, no other gate variables. |

The helper signature is `_run_validation_gates(task, verdict, lifecycle, *, caller_agent=None) -> None`. It dispatches on `(verdict, lifecycle, caller_agent)` to choose the gate set. Tests cover (a) every cell in the gate-set table, (b) cross-product of lifecycle × verdict, and (c) `caller_agent` resolution from session context when not passed explicitly.

**Acceptance:**

- 1.8.1 — Lifecycle transitions in review tools is implemented according to this section. file: `src/gobby/storage/tasks/_transitions.py`.
- 1.8.2 — `mark_task_review_approved` on a leaf at `lifecycle=in_development` advances to `lifecycle=holistic_review, status=review_approved` atomically (DB transaction + `task_lifecycle_events` row in one commit). Leaf does NOT close. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_qa_review_transitions.py::test_leaf_approve_advances_to_holistic`.
- 1.8.3 — `mark_task_review_rejected` on a leaf at `lifecycle=in_development` increments `qa-attempts:N` label, sets `status=open`, appends rejection_notes, leaves `lifecycle=in_development`. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_qa_review_transitions.py::test_leaf_reject_increments_qa_attempts`.
- 1.8.4 — `mark_task_review_rejected(epic, lifecycle=holistic_review)` without `cited_subtasks` raises a validation error (R4.F5 invariant preserved). The escalate-rescope third path via `escalate_task(epic, reason="needs_human:rescope_required:..."|"needs_human:requirements_unclear:...")` is the only no-cited rejection mechanism. test: `tests/storage/test_holistic_rejection.py::test_bare_rejection_invalid_and_escalate_rescope_path`.
- 1.8.5 — `mark_task_review_rejected(epic, lifecycle=holistic_review, cited_subtasks=[leaf_ids])` atomically rewinds: cited leaves go from `(holistic_review, review_approved)` (or `(holistic_review, closed)` if cascade ran) to `(in_development, open)`; epic goes to `(in_development, open)`. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_holistic_rejection.py::test_cited_rewinds_leaves_and_epic`.
- 1.8.6 — `_run_validation_gates(task, verdict, lifecycle, *, caller_agent=None)` helper signature lands in `_transitions.py` and is called from each `mark_task_*` / `escalate_task` entry-point. The helper dispatches on `(verdict, lifecycle, caller_agent)` to choose one of the three gate sets covered by 1.8.6a–1.8.6c below. Cross-product test in `tests/storage/test_mark_task_review_validation.py::test_full_verdict_lifecycle_matrix` asserts the right gate set fires for every reachable `(verdict, lifecycle)` combination. file: `src/gobby/storage/tasks/_transitions.py`. (Cross-reference §2.14a.5.)
- 1.8.6a — **leaf-close** gate set: `mark_task_review_approved` on a leaf at `lifecycle=in_development` (qa-reviewer caller; the only verdict path that ships code on the approved task) runs the full close-equivalent gates: `commit-attached`, `validation_criteria` pass, `errors_resolved`, `memory_review_completed`. `skip_validation` is rejected (silently stripped is not enough — the gate explicitly fails if it's set on a build-agent caller). file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_mark_task_review_validation.py::test_leaf_close_gate_set` covers (a) full gate set fires on qa approval of an in_development leaf, (b) approval blocked when commit is missing, (c) approval blocked when validation_criteria fails, (d) `skip_validation` rejected.
- 1.8.6b — **stage-advance** gate set: `mark_task_review_approved` on a task at `lifecycle ∈ {plan_review, test_arch, expanding, holistic_review, merging}` runs stage-specific output gates ONLY (manifest emitted / test architecture appended / expansion run completed / leaves in terminal-or-holistic / merge clean) and DOES NOT require commit-attached or validation_criteria on the approving task. Each lifecycle's specific gate is documented in §1.8 prose. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_mark_task_review_validation.py::test_stage_advance_gate_set` covers each lifecycle's stage-specific gate firing and asserts that approval succeeds without commit-attached when the stage-specific output is present (closing the prior single-gate-set deadlock).
- 1.8.6c — **light** gate set: `mark_task_review_rejected` and `escalate_task` at any lifecycle run only valid claim/session + `rejection_notes`/`reason` non-empty. Rejection MUST succeed when commits are missing or validation_criteria fails — that is precisely the state the reviewer is reporting. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/test_mark_task_review_validation.py::test_light_gate_set` covers (a) rejection succeeds with a missing commit, (b) rejection succeeds with failing validation_criteria when rejection_notes is non-empty, (c) escalation succeeds the same way, (d) light gate fails when claim/session is invalid or notes/reason is empty.
- 1.8.7 — Extended `de_escalate_task(task_id, target_status, lifecycle, reason)` performs the combined status + lifecycle change in a single transaction; `task_lifecycle_events` records both rows (de-escalate and lifecycle advance) atomically. test: `tests/storage/test_de_escalate.py::test_combined_status_lifecycle`.

### 1.9 Dispatcher scanner [category: code] (depends: 1.4, 1.6, 1.7, 1.8, 2.8b)

`kind: deliverable`

Target: `src/gobby/dispatch/dispatcher.py` (new file)

> **Cross-phase dependency note** (R7.F-1.9-2.8b-ordering): §1.9 lives in Phase 1 but depends on §2.8b in Phase 2 because the dispatcher's `StartExpansionRun` handler imports `start_expansion_run_impl` from `gobby.mcp_proxy.tools.tasks._expansion`, and §2.8b is the deliverable that exports that symbol. The expansion-qa contract surfaces (the live tooling that the impl wraps) already exist in the repo; §2.8b only adds the explicit `_impl` export with the dependency-injected signature. §1.9's `_dispatch(StartExpansionRun)` arm cannot be implemented (or tested end-to-end) until §2.8b lands. Phase 1 ordering still makes architectural sense — the dispatcher engine is foundation work — but the dependency annotation makes the cross-phase constraint explicit so the deterministic compile path schedules §2.8b before §1.9's TDD-impl leaves.

```python
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
import json

from gobby.agents.spawn_executor import SpawnRequest, execute_spawn
from gobby.dispatch import mutex, rules
from gobby.dispatch.actions import (
    AdvanceLifecycle, AppendAuditMarker, ArchivePlan, CascadeCloseLeaves,
    CreateClone, CreateWorktree, EmitStubManifest, EscalateTask,
    MarkTaskReviewApproved, Skip, SpawnAgent, StartExpansionRun,
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
    # `swept` removed (R7.F-sweep-overlap): startup mutex reconciliation lives
    # in `runner_init.py`'s one-shot `sweep_mutexes_once(db)` call, not per-tick.
    dispatched: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

MAX_ACTIVE_AGENTS_DEFAULT = 10   # configurable via daemon config
DISPATCHER_LOG = Path.home() / ".gobby" / "logs" / "dispatcher.jsonl"

@dataclass
class DispatcherServices:
    """R7.F-dispatcher-services: explicit dependency container for the
    dispatcher's daemon-side state. The Round 9 `start_expansion_run_impl`
    fix made expansion's dependencies explicit; this round threads them
    end-to-end so both cron ticks (§1.10) and immediate build-triggered
    ticks (`_kick_dispatcher_tick`, §3.2) use the same wired services.
    No private global helpers — every dependency arrives at `run_tick` via
    this object.

    R7.F-build-config-on-services: `build_config` is a first-class field
    so `_kick_dispatcher_tick` can read `max_active_agents` from a live
    `BuildConfig` surface. The container's `config` field is `DaemonConfig`
    (Pydantic model with no `.get()`); dispatch knobs live on `BuildConfig`,
    so we carry both: `config` for daemon-wide settings, `build_config` for
    the dispatch knobs."""
    db: LocalDatabase
    task_manager: LocalTaskManager
    llm_service: LLMService
    config: DaemonConfig
    build_config: BuildConfig
    completion_registry: CompletionEventRegistry  # live class name; no `CompletionRegistry`
    session_manager: ChildSessionManager  # live SpawnRequest dependency (R7.F-spawn-deps)
    machine_id: str                        # live SpawnRequest dependency
    triggering_session_id: str             # storage UUID returned by register_session (NOT the external_id we passed in — see §1.10.4 R7.F-storage-uuid)
    parent_session_id: str                 # dispatcher's parent session for spawned agents — same value as triggering_session_id


async def run_tick(services: DispatcherServices, holder: str, max_active: int) -> TickReport:
    """Per-tick dispatcher. Concurrency-safe under overlapping ticks (cron + an
    immediate `_kick_dispatcher_tick` from `gobby build`): does NOT call
    `mutex.sweep_on_startup` here — that's a daemon-boot reconciliation owned
    by `runner_init.py` (§1.10) which runs exactly once before the first tick.
    Calling it per-tick (R7.F-sweep-overlap) would let an overlapping tick
    delete a live non-spawn mutex row and double-dispatch the same task.

    `services` carries the live daemon dependencies the dispatch path needs
    (per R7.F-dispatcher-services). All `_dispatch` arms read from `services`
    instead of calling module-level helpers."""
    db = services.db
    report = TickReport(started_at=datetime.now(UTC).isoformat())
    active = _count_active_autonomous_agents(db)
    for task in list_automation_candidates(db):
        if active >= max_active:
            report.skipped.append((task.id, "agent slot cap reached"))
            continue
        action = rules.evaluate(task)
        if isinstance(action, Skip):
            report.skipped.append((task.id, action.reason))
            continue
        # Acquire mutex for the duration of evaluate-and-act. The pre-lock
        # action is used ONLY to choose the lock kind/TTL; the canonical action
        # (and its kind, agent_run_id) is recomputed AFTER the lock to close
        # the TOCTOU window per R7.F-toctou-action-class.
        provisional_action = action if not isinstance(action, list) else action[-1]
        provisional_kind = _action_kind(provisional_action)
        with mutex.acquire(db, task.id, holder, provisional_kind, agent_run_id=None) as (ok, mutex_token):
            if not ok:
                report.skipped.append((task.id, f"mutex contended ({provisional_kind})"))
                continue
            # Re-evaluate under lock to close the TOCTOU window:
            fresh = _reload_task(db, task.id)
            action = rules.evaluate(fresh)
            if isinstance(action, Skip):
                report.skipped.append((task.id, action.reason))
                continue
            actions = action if isinstance(action, list) else [action]
            primary_action = actions[-1]
            kind = _action_kind(primary_action)
            # R7.F-toctou-action-class: if the locked re-evaluation produced a
            # different action class than the pre-lock evaluation, the mutex
            # we hold is for the wrong kind (e.g. 30s lifecycle TTL when we
            # actually need a 600s spawn lease). Skip and let the next tick
            # re-acquire with the correct kind. This avoids: (a) running a
            # spawn under a non-spawn mutex row that expires before the agent
            # claims the task, and (b) running a non-spawn action under a
            # long-lived spawn lease that blocks subsequent ticks.
            if kind != provisional_kind:
                report.skipped.append((task.id, f"action class changed under lock ({provisional_kind}→{kind}); retry next tick"))
                continue
            # Now allocate the agent_run_id from the FRESH action so the
            # mutex row's run_id aligns with the actual spawn (event-handler
            # cleanup keys on this).
            agent_run_id = f"run-{uuid4().hex[:12]}" if isinstance(primary_action, SpawnAgent) else None
            if agent_run_id is not None:
                # Update the mutex row's run_id to match the freshly-allocated
                # spawn id. acquire() initially wrote run_id=None; this update
                # is in the same logical "lock acquired" window.
                mutex.attach_run_id(db, task.id, agent_run_id, holder)
            try:
                for a in actions:
                    await _dispatch(services, fresh, a, agent_run_id, report)
                if any(isinstance(a, SpawnAgent) for a in actions):
                    # R7.F-mutex-detach: pass the per-acquire token, not task_id.
                    # Detach state is consumed on scope exit (see acquire's finally).
                    mutex.detach_from_context(mutex_token)
                    active += 1
            except Exception as exc:
                report.errors.append((task.id, str(exc)))
    report.finished_at = datetime.now(UTC).isoformat()
    _persist_tick_report(report)
    return report

async def _dispatch(services: DispatcherServices, task, action, agent_run_id, report):
    db = services.db
    match action:
        case SpawnAgent() as s:
            prompt, initial_vars = PROMPT_BUILDERS[s.prompt_builder](task)
            req = SpawnRequest(
                prompt=prompt,
                # R7.F-cwd-agent: pass agent name so _resolve_cwd can route
                # the merge agent to the source repo regardless of isolation
                # (per the helper contract documented below). Dev/QA leaves
                # take the parent epic's isolation artifact.
                cwd=_resolve_cwd(task, s.agent),
                provider=_resolve_provider(s),
                session_id=str(uuid4()),
                run_id=agent_run_id,
                agent_run_id=agent_run_id,
                # R7.F-spawn-deps: every spawn dependency comes from `services`,
                # not from private global helpers. The dispatcher session is
                # the parent for all dispatched agents — same UUID, same row.
                parent_session_id=services.parent_session_id,
                project_id=task.project_id,
                session_manager=services.session_manager,
                machine_id=services.machine_id,
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
        case StartExpansionRun(task_id):
            # R6.F5 / R7.F2 / R7.F-tdd-removed / R7.F-impl-deps / R7.F-dispatcher-services:
            # call the dependency-injected `start_expansion_run_impl` (§2.8b)
            # with the daemon services threaded through `services`. The MCP
            # closure path is unchanged — this is purely an in-process
            # additional caller. NOTE: TDD shape is per-manifest-entry per
            # §2.21; no epic-level `tdd` kwarg. On compile-side exceptions,
            # the impl surfaces a failed run via its standard error path; we
            # still persist the id so `rule_validate_expansion` and
            # `expansion-qa` (§2.9) can observe failure and reject (per the
            # `_expansion_run_terminal` predicate that covers `completed | failed`).
            from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl
            from gobby.storage.tasks._artifacts import set_artifact
            run = await start_expansion_run_impl(
                task_manager=services.task_manager,
                llm_service=services.llm_service,
                config=services.config,
                completion_registry=services.completion_registry,
                triggering_session_id=services.triggering_session_id,
                task_id=task_id,
                plan_file=_plan_file_path(db, task_id) if _has_plan_file(db, task_id) else None,
                auto_apply=True,
                force_new=False,
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
        case AdvanceLifecycle(task_id, to, reason, by_actor, status):
            # R7.F-advance-status: forward the optional `status` so yolo
            # cap-exhaustion paths can land at their explicit terminal status
            # (merging→merged status="closed"; in_development→holistic_review
            # status="review_approved"). When status=None the helper applies
            # the (to, task_type) default per §1.8.
            advance_lifecycle(db, task_id, to, reason=reason, by_actor=by_actor,
                              status=status)
            report.dispatched.append(task_id)
        case EscalateTask(task_id, reason):
            from gobby.storage.tasks._transitions import escalate_task
            escalate_task(db, task_id, reason=reason)
            report.dispatched.append(task_id)
        case AppendAuditMarker(task_id, heading, body):
            append_description_section(db, task_id, heading=heading, body=body)
            # Not reported as "dispatched" — purely an audit side-effect.
        case MarkTaskReviewApproved(task_id, by_actor, approval_notes):
            # NOTE (R7.F-stale-prose): no rule currently emits this action —
            # yolo qa-cap exhaustion uses `AdvanceLifecycle(..., status=
            # "review_approved")` instead so it bypasses the leaf-close gate
            # that would re-evaluate the failing validation (§1.7 rule_qa,
            # §1.8.6a). The arm is retained for forward compatibility: a
            # future caller that wants the leaf-close gate to run on a
            # dispatcher-emitted approval can route through here. Goes
            # through `_transitions.mark_task_review_approved`, so the gate
            # set is resolved per §1.8.6 with `caller_agent="dispatcher"`.
            from gobby.storage.tasks._transitions import mark_task_review_approved
            mark_task_review_approved(db, task_id, approval_notes=approval_notes,
                                      by_actor=by_actor)
            report.dispatched.append(task_id)
        case CascadeCloseLeaves(epic_task_id, leaf_task_ids):
            # R7.F-dispatch-exhaustiveness: emitted by `rule_cascade_close_on_merge`
            # when an epic reaches `lifecycle=merged` and audit-pending leaves
            # need to cascade to `closed`. Atomic per-leaf close in a single
            # transaction; already-closed leaves are filtered upstream by the
            # rule's `_leaves_pending_cascade_close` helper but the writer
            # double-checks idempotently.
            from gobby.storage.tasks._transitions import cascade_close_leaves
            cascade_close_leaves(db, epic_task_id, leaf_task_ids)
            report.dispatched.append(epic_task_id)
        case EmitStubManifest(task_id, by_actor):
            # R7.F-yolo-manifest-fallback / R7.F-no-livelock: this arm NEVER
            # raises. The yolo cap-exhausted sequence in §1.7 returns
            # `[AppendAuditMarker, EmitStubManifest, AdvanceLifecycle(test_arch)]`
            # and the AdvanceLifecycle MUST run deterministically — raising
            # would leave the task at plan_review for the next tick which
            # would re-emit the audit marker and re-attempt the same failure
            # (livelock). Failures are absorbed via audit markers; downstream
            # `gobby expand` rejection at expansion time is the documented
            # escape hatch when a plan is genuinely unsalvageable.
            from gobby.plans.manifest_emitter import emit_stub_manifest
            from gobby.storage.tasks._artifacts import get_artifact
            from gobby.storage.tasks._transitions import append_description_section
            plan_file_path = get_artifact(db, task_id, "plan_file_path")
            if plan_file_path:
                # The §2.21a emitter never raises — its `EmitOutcome` literal
                # covers every failure mode internally (audit-section appended
                # in-file on unsalvageable plans). The dispatcher just calls it.
                emit_stub_manifest(plan_file_path, by_actor=by_actor)
            else:
                # Absorb missing-artifact failure as an audit marker on the
                # planning anchor; lifecycle advance still runs as the next
                # action in the parent rule's sequence. No raise.
                append_description_section(
                    db, task_id, heading="Yolo Fallbacks",
                    body=f"{_now_iso()}: plan_file_path artifact missing on yolo cap exhaustion; "
                         f"force-advancing without manifest emission. "
                         f"Downstream gobby expand will reject this plan, "
                         f"requiring human intervention at expansion time.",
                )
            report.dispatched.append(task_id)
        case ArchivePlan(epic_task_id, plan_id):
            # R7.F-dispatch-exhaustiveness: emitted by `rule_archive_plan_on_merge`
            # when the epic terminally closes. Routes to the §2.15/§2.18
            # plan-management API which atomically moves the plan file to
            # `.gobby/plans/completed/`, flips the DB plan-state to archived,
            # and removes the coverage manifest.
            from gobby.storage.plans import LocalPlanManager
            LocalPlanManager(db).archive_plan(plan_id, reason="epic merged")
            report.dispatched.append(epic_task_id)

def _persist_tick_report(report: TickReport) -> None:
    """Append a structured line to ~/.gobby/logs/dispatcher.jsonl.
    Caller must handle rotation (planned as a future daemon-level concern;
    first cut just appends indefinitely and lets logrotate handle size)."""
    DISPATCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DISPATCHER_LOG.open("a") as fh:
        fh.write(json.dumps(asdict(report)) + "\n")
```

`_action_kind` maps each `Action` subclass to an `ActionKind` for the mutex per the table below; missing entries raise `KeyError` so adding a new action without updating the map fails loud rather than silently picking a default. `MarkTaskReviewApproved`, `CascadeCloseLeaves`, and `ArchivePlan` map to `"lifecycle"` (short TTL, same class as `AdvanceLifecycle`); `CreateClone` and `CreateWorktree` both map to `"worktree"` (longer TTL — same TTL/contention class); `SpawnAgent` maps to `"spawn"`; `StartExpansionRun` maps to `"expansion"`; `AppendAuditMarker` and `EmitStubManifest` both map to `"field"` (file/description side-effect class — `EmitStubManifest` writes the plan file in place; same TTL semantics as audit markers); `EscalateTask` maps to `"lifecycle"`.

`_build_isolation_handler(db, handler_cls, epic_task_id)` constructs the appropriate handler with its real dependencies. Constructor argument order matches the live signatures in `src/gobby/agents/isolation.py` (R7.F-isolation-ctor): `WorktreeIsolationHandler(git_manager, worktree_storage)` (git manager FIRST, then storage) and `CloneIsolationHandler(clone_manager, clone_storage, git_manager=None)` (clone manager FIRST, then storage; the optional `git_manager` is the source-repo git manager for branch detection). The dependencies pull from existing daemon services: `WorktreeGitManager(repo_path)` + `db.local_worktree_manager` for the worktree handler; `CloneGitManager(repo_path)` + `db.local_clone_manager` + the source-repo `GitManager` for the clone handler. The dispatcher test for §1.9 (`tests/dispatch/test_dispatcher_isolation_handler_args.py`) mocks both classes and asserts the positional argument bindings to lock the order against silent regressions. `_build_spawn_config(db, epic_task_id, *, base_branch)` fills a `SpawnConfig` (`src/gobby/agents/isolation.py`) with `project_id`, `project_path`, `provider`, `task_id`, `base_branch`, and any other fields the handlers' branch-name generation requires. `_resolve_cwd(task, agent_name)` returns the appropriate working directory for the agent. **Dev/QA agents** running on a leaf get the parent epic's isolation artifact: `task_artifacts.clone_path` when `task.isolation == clone`, `task_artifacts.worktree_path` when `worktree`, repo root when `none`; the resolution walks up to `_parent_epic(task)` for leaves. **The merge agent** is the exception (R6.F3): it runs in the source repo regardless of isolation, because the `gobby-worktrees:merge_worktree` and `gobby-clones:merge_clone` tools manage paths internally and require the source-repo cwd. So when `agent_name == "merge"`, `_resolve_cwd` returns the repo root unconditionally; the dispatcher passes `worktree_id` / `clone_id` and `target_branch` as `initial_variables` instead. `_clones_dir()` reads `BuildConfig.clones_dir` (§3.1, default `~/.gobby/clones/`). `_count_active_autonomous_agents` queries `running_agents` for sessions flagged autonomous (the spawn path tags them). `_reload_task` re-queries the row under the mutex to avoid TOCTOU. `append_description_section` in `_transitions.py` is a new helper that appends a `## {heading}\n{body}\n` block to the task description (idempotent by (task_id, heading, body) signature — duplicate markers within the same tick are deduped).

**Acceptance:**

- 1.9.1 — Dispatcher scanner is implemented according to this section. file: `src/gobby/dispatch/dispatcher.py`.
- 1.9.2 — `_dispatch` is exhaustive over every `Action` subclass from §1.6: `SpawnAgent`, `StartExpansionRun`, `CreateWorktree`, `CreateClone`, `AdvanceLifecycle`, `EscalateTask`, `AppendAuditMarker`, `EmitStubManifest`, `MarkTaskReviewApproved`, `CascadeCloseLeaves`, `ArchivePlan`. Missing match arms (or unimported action classes) fail at import time, not at runtime. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_dispatcher_exhaustiveness.py` constructs an instance of each Action subclass and runs it through `_dispatch` with mocked downstream side-effects, asserting (a) the call returns without `match` exhausting, (b) the documented downstream effect fires (mark_task_review_approved called with notes for `MarkTaskReviewApproved`; cascade_close_leaves invoked for `CascadeCloseLeaves`; LocalPlanManager.archive_plan called for `ArchivePlan`; `gobby.plans.manifest_emitter.emit_stub_manifest` called with the plan file path for `EmitStubManifest`), and (c) every dispatched action's report row is populated correctly.
- 1.9.5 — TOCTOU-aware action-class handling: `run_tick` recomputes `_action_kind` from the locked re-evaluation's action and skips the dispatch if the kind differs from the pre-lock kind (the mutex we hold is for the wrong TTL/contention class). `agent_run_id` is allocated from the FRESH action, not the pre-lock action, and attached to the mutex row via `mutex.attach_run_id` so the spawn-event-handler cleanup (§1.5) keys on the right id. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_dispatcher_toctou.py` covers (a) pre-lock evaluation = `AdvanceLifecycle`, locked re-evaluation = `SpawnAgent` → dispatcher skips with the action-class-changed reason; (b) pre-lock = `SpawnAgent`, locked = `Skip` → dispatcher skips per Skip's reason without dispatching; (c) pre-lock = locked = `SpawnAgent` → dispatcher allocates a fresh `agent_run_id`, attaches it via `mutex.attach_run_id`, and spawns; (d) pre-lock = locked = `AdvanceLifecycle` → dispatcher proceeds without allocating a run_id (none needed for non-spawn actions).
- 1.9.3 — `_resolve_cwd(task, agent_name: str)` is the documented two-argument signature. The `SpawnAgent` dispatch path passes `s.agent` so the merge agent (`agent_name="merge"`) routes to the source repo regardless of `task.isolation`, while dev/qa leaves take the parent epic's `worktree_path` or `clone_path`. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_dispatcher_cwd_resolution.py` covers (a) `SpawnAgent(agent="merge")` on an epic with `isolation=worktree` produces `SpawnRequest.cwd = repo_root` and `worktree_id` arrives via initial variables; (b) `SpawnAgent(agent="backend-developer")` on a leaf with parent `isolation=worktree` produces `SpawnRequest.cwd = parent.worktree_path`; (c) parallel coverage for `isolation=clone`; (d) `isolation=none` returns repo root for any agent.
- 1.9.4 — `_action_kind` exhaustive map per the table above; missing action raises `KeyError` immediately. test: `tests/dispatch/test_action_kind_map.py` covers each `Action` subclass returning the documented kind and asserts the map is locked (adding a new action without updating the map breaks the test).

### 1.10 Cron handler registration [category: code] (depends: 1.9)

`kind: deliverable`

Target: `src/gobby/dispatch/cron_registration.py` (new) + `src/gobby/runner_init.py` (add call)

```python
# src/gobby/dispatch/cron_registration.py
from gobby.config.build import BuildConfig  # max_active_agents, dispatch_interval_seconds live here
from gobby.dispatch import mutex
from gobby.dispatch.dispatcher import DispatcherServices, run_tick
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage  # live cron storage module
from gobby.storage.cron_models import CronJob

def register_state_dispatcher(
    executor: CronExecutor,
    services: DispatcherServices,
    build_config: BuildConfig,
) -> None:
    """R7.F-dispatcher-services / R7.F-live-config: takes a `DispatcherServices`
    instance + the live `BuildConfig` (which carries `max_active_agents` and
    `dispatch_interval_seconds`). Both cron handlers and immediate
    `_kick_dispatcher_tick` calls (§3.2) consume the same services instance,
    so the expansion-impl path is wired identically in both surfaces."""
    max_active = build_config.max_active_agents
    async def handler(job: CronJob) -> str:
        holder = f"state-dispatcher:{job.id}"
        report = await run_tick(services, holder, max_active)
        return (f"dispatched={len(report.dispatched)} "
                f"skipped={len(report.skipped)} errors={len(report.errors)}")
    executor.register_handler("state-dispatcher", handler)

def ensure_state_dispatcher_cron_row(
    cron_storage: CronJobStorage, project_id: str, build_config: BuildConfig,
) -> None:
    """Idempotent: insert a `cron_jobs` row via the live `CronJobStorage.create_job`
    API (`schedule_type="interval"`, `interval_seconds=...`, `action_config={"handler": "state-dispatcher"}`).
    No-op if a row with the same `name` already exists on this project."""
    cron_storage.create_job(
        project_id=project_id,
        name="state-dispatcher",
        schedule_type="interval",
        interval_seconds=build_config.dispatch_interval_seconds,
        action_type="handler",
        action_config={"handler": "state-dispatcher"},
        enabled=True,
    )

def sweep_mutexes_once(db) -> int:
    """R7.F-sweep-overlap: daemon-boot mutex reconciliation. Called exactly
    once per daemon process during startup, BEFORE any tick (cron or
    `_kick_dispatcher_tick`) can fire. Removed from `run_tick` to prevent
    overlapping ticks from deleting live non-spawn mutex rows."""
    return mutex.sweep_on_startup(db)
```

In `runner_init.py`, after `ServiceContainer` instantiation (the live
`set_app_context(services)` call near the end of `init_servers`) — actually
before that, since dispatcher services need to be on the container the cron
handler reads via `get_app_context()`. Concretely: build `DispatcherServices`,
add it to `ServiceContainer` before `set_app_context`, then register the cron
handler.

```python
from gobby.app_context import ServiceContainer, set_app_context
from gobby.dispatch.cron_registration import (
    ensure_state_dispatcher_cron_row, register_state_dispatcher, sweep_mutexes_once,
)
from gobby.dispatch.dispatcher import DispatcherServices

# R7.F-sweep-overlap: do the mutex reconciliation ONCE here, before any tick
# can fire. Per-tick sweep is removed from run_tick to avoid live-row deletion
# under overlapping ticks (cron + _kick_dispatcher_tick from gobby build).
sweep_mutexes_once(runner.database)

# R7.F-dispatcher-services / R7.F-live-config / R7.F-build-config-on-services /
# R7.F-load-config-signature: build the dependency container from the live
# `runner` attributes and the parsed `BuildConfig`. Reused for both cron
# handlers AND immediate build-triggered ticks via
# `ServiceContainer.dispatcher_services`.
from gobby.config.build import load_build_config

# `load_build_config(project_root, flag_overrides=None)` is the live signature;
# it walks `~/.gobby/build.yaml`, `<project_root>/.gobby/build.yaml`, and any
# CLI flag overrides. The dispatcher's daemon-side load uses no flag overrides
# (overrides are per-build and persisted to task_artifacts at build time per
# §2.19; the dispatcher reads from artifacts via _resolve_retry_cap, not from
# the static BuildConfig). project_root is resolved from the live git manager:
project_root = runner.git_manager.repo_path
build_config = load_build_config(project_root)

# Synthetic dispatcher session id (R7.F-dispatcher-session / R7.F-storage-uuid):
# the daemon allocates one Gobby session row per process startup with
# `source="dispatcher"` and an explicit
# `external_id="dispatcher-<machine_id>-<boot_timestamp>"`. The session exists
# for triggering-session traceability on dispatched expansion runs and any
# other audit-trail surfaces; it never claims tasks. Constructed in
# `runner_init.py` via:
#
#     storage_uuid = runner.session_manager.register_session(
#         external_id=f"dispatcher-{machine_id}-{boot_timestamp}",
#         machine_id=machine_id,
#         source="dispatcher",
#         project_id=runner.project_id,
#     )
#
# The live `register_session` returns the **storage UUID** (the `sessions(id)`
# row primary key) — NOT the `external_id` we passed in. The expansion-runs
# table foreign-keys `triggering_session_id` to `sessions(id)`, so we MUST
# capture and propagate the storage UUID, not the external id. On failure
# the live API returns a temporary UUID and logs; the dispatcher refuses to
# start (raises) if the returned id is not present in `sessions(id)` — an
# unresolvable triggering session would write expansion-run rows with
# dangling FK refs. The captured storage UUID is stored on
# `runner.dispatcher_session_id: str`:
dispatcher_session_id = runner.dispatcher_session_id  # storage UUID, not external_id

dispatcher_services = DispatcherServices(
    db=runner.database,
    task_manager=runner.task_manager,
    llm_service=runner.llm_service,
    config=runner.config,                            # DaemonConfig (Pydantic model)
    build_config=build_config,                       # BuildConfig with dispatch knobs
    completion_registry=runner.completion_registry,  # CompletionEventRegistry
    session_manager=runner.session_manager,          # ChildSessionManager (R7.F-spawn-deps)
    machine_id=runner.machine_id,
    triggering_session_id=dispatcher_session_id,     # storage UUID
    parent_session_id=dispatcher_session_id,         # same row — dispatcher is parent for all spawned agents
)

# Add to ServiceContainer (new field, see §1.10.2 acceptance) BEFORE set_app_context
# so `get_app_context().dispatcher_services` works for `_kick_dispatcher_tick`.
services = ServiceContainer(
    # ... all existing fields per the live `init_servers` constructor call ...
    dispatcher_services=dispatcher_services,
)
set_app_context(services)

register_state_dispatcher(runner.cron_executor, dispatcher_services, build_config)
ensure_state_dispatcher_cron_row(runner.cron_storage, runner.project_id, build_config)
```

The cron_jobs row, per the live `CronJobStorage.create_job` API (R7.F-live-cron-api — `schedule_expression` does not exist as a field; the live shape is `schedule_type` + `interval_seconds` for interval jobs, or `schedule_type` + `cron_expression` for cron jobs):

```python
cron_storage.create_job(
    project_id=project_id,
    name="state-dispatcher",
    schedule_type="interval",
    interval_seconds=build_config.dispatch_interval_seconds,  # default 60
    action_type="handler",
    action_config={"handler": "state-dispatcher"},
    enabled=True,
)
```

**Acceptance:**

- 1.10.1 — Cron handler registration is implemented according to this section, using the live `CronJobStorage.create_job(project_id, name, schedule_type="interval", interval_seconds=..., action_type="handler", action_config={"handler": "state-dispatcher"})` shape. file: `src/gobby/dispatch/cron_registration.py`. test: `tests/dispatch/test_cron_registration.py` covers (a) handler registered under name "state-dispatcher", (b) cron row inserted with the live field shape (no `schedule_expression`), (c) idempotent — re-running on existing row is a no-op, (d) `interval_seconds` matches `BuildConfig.dispatch_interval_seconds`.
- 1.10.5 — `GobbyRunner` exposes the `CronExecutor` instance as `runner.cron_executor` (R7.F-cron-executor-attr). The live runner already constructs the executor inside `init_orchestration` as a local; this acceptance lifts it onto the `runner` object so `init_servers` can wire dispatcher registration against the same instance the `cron_scheduler` uses. file: `src/gobby/runner.py`, `src/gobby/runner_init.py`. test: `tests/runner_init/test_cron_executor_attr.py` asserts `runner.cron_executor` is the same instance attached to `runner.cron_scheduler`.
- 1.10.2 — `ServiceContainer` (live at `src/gobby/app_context.py`) gains a `dispatcher_services: DispatcherServices` field; `runner_init.py` constructs `DispatcherServices` from `runner` attributes (`runner.database`, `runner.task_manager`, `runner.llm_service`, `runner.config`, `runner.completion_registry`, `runner.session_manager`, `runner.machine_id`, `runner.dispatcher_session_id` (the storage UUID returned by `register_session`)) plus a freshly-loaded `BuildConfig` from `load_build_config(runner.git_manager.repo_path)` (R7.F-load-config-signature: live signature is `load_build_config(project_root, flag_overrides=None)`, NOT `(db, project_id)`). Adds the result to `ServiceContainer` before `set_app_context(services)`. file: `src/gobby/app_context.py`, `src/gobby/runner_init.py`. test: `tests/runner_init/test_dispatcher_services_wiring.py` covers (a) `ServiceContainer.dispatcher_services` is populated after `init_servers`, (b) `get_app_context().dispatcher_services` returns the same instance, (c) `_kick_dispatcher_tick` (§3.2) reads through this surface and constructs no fresh services, (d) `load_build_config` is called with `runner.git_manager.repo_path` (path-based) — NOT with `(db, project_id)`, (e) every `SpawnRequest` dependency (`session_manager`, `machine_id`, `parent_session_id`) is resolved from `services` — the dispatcher source contains no private global helpers like `_session_manager()` or `_machine_id()` (regression locked via `gcode search-content "_session_manager\|_machine_id\|_dispatcher_parent_session"` returning empty inside `src/gobby/dispatch/`).
- 1.10.4 — Synthetic dispatcher session (R7.F-dispatcher-session / R7.F-storage-uuid): `runner_init.py` registers exactly one Gobby session per daemon process at boot via the live `LocalSessionManager.register_session(external_id="dispatcher-<machine_id>-<boot_timestamp>", machine_id=..., source="dispatcher", project_id=...)`. The live API returns `str` — the **storage UUID** (the `sessions(id)` row primary key), NOT the `external_id` we passed in. We capture and store the **storage UUID** on `runner.dispatcher_session_id: str`; the external_id is what the live API uses internally to dedupe registrations. `DispatcherServices.triggering_session_id` and `parent_session_id` carry the storage UUID directly because `expansion_runs.triggering_session_id` foreign-keys `sessions(id)`. On failure the live API returns a temporary UUID; the dispatcher MUST refuse to start (raises `RuntimeError`) if the returned id is not present in `sessions(id)` — an unresolvable triggering session would write expansion-run rows with dangling FK refs and corrupt the audit trail. file: `src/gobby/runner.py` (add `dispatcher_session_id: str` attribute), `src/gobby/runner_init.py` (call `register_session` once at boot, capture return value, validate FK presence). test: `tests/runner_init/test_dispatcher_session.py` covers (a) exactly one row is created per daemon boot, (b) source is `"dispatcher"`, (c) `runner.dispatcher_session_id` matches the persisted `sessions.id` (storage UUID), NOT the external_id we passed in, (d) the session never claims a task (assertion via `claim_task` permission rule), (e) explicit failure path: when register_session returns a temporary UUID not present in `sessions(id)`, daemon startup raises rather than continue with a dangling FK.
- 1.10.3 — `register_state_dispatcher(executor, services, build_config)` reads `max_active_agents` from the `BuildConfig` directly (not from a `DaemonConfig.get(...)` call — `DaemonConfig` is a Pydantic model with no `.get` method). file: `src/gobby/dispatch/cron_registration.py`. test: `tests/dispatch/test_cron_registration.py::test_register_uses_build_config_max_active`.

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
- 2.8.4 — Expansion initializes every generated leaf at `lifecycle=in_development` so `rule_dispatch_leaf` (§1.7) can pick it up on the next tick. The legacy `lifecycle_stage` column is left at its default; only `lifecycle` (the new dispatch field on every task per the Constraints update) is set. Epic itself reaches `lifecycle=in_development` either by `gobby build` (skip-stage path) or by the test_arch/expanding rule chain. file: `src/gobby/tasks/expansion_service.py`. test: `tests/tasks/test_expansion_lifecycle_init.py` covers (a) every generated leaf has `lifecycle=in_development` after a successful expansion run, (b) generated leaves do NOT have any value written to `lifecycle_stage` (kept at default), (c) regression: a leaf left at default `lifecycle=open` would fail dispatch — explicit assertion that no leaf is at `open` post-expansion.

### 2.8b Expose `start_expansion_run_impl` for in-process dispatcher use [category: code]

`kind: deliverable`

> **Status: partial** — nested MCP handler `start_expansion_run` shipped via commit `4ee54dc5` at `src/gobby/mcp_proxy/tools/tasks/_expansion.py`; the importable `start_expansion_run_impl` symbol required by §1.9 is not exported.

Target: `src/gobby/mcp_proxy/tools/tasks/_expansion.py` (or wherever the MCP tool's handler lives)

R6.F5 + R7.F2 + R7.F-impl-deps fix. The existing `gobby-tasks-ops:start_expansion_run` MCP tool is a registry closure inside `RegistryContext` — it depends on `task_manager`, `llm_service`, daemon `config`, the completion registry, and `_resolve_current_session(ctx)` for the triggering session id. The MCP transport layer wires those dependencies; the dispatcher does not have a `RegistryContext` available, only `db` + `task`. Exporting the closure verbatim is not possible; this task pins a **dependency-injected impl signature** the dispatcher can call directly.

**`start_expansion_run_impl` signature** (locked by §2.8b.1, used by §1.9):

```python
async def start_expansion_run_impl(
    *,
    task_manager: LocalTaskManager,
    llm_service: LLMService,
    config: DaemonConfig,
    completion_registry: CompletionEventRegistry,  # live class name; no `CompletionRegistry`
    triggering_session_id: str,
    task_id: str,
    plan_file: str | None = None,
    auto_apply: bool = True,
    force_new: bool = False,
    provider: str | None = None,
    model: str | None = None,
    project: str | None = None,
) -> ExpansionRun:
    """Dependency-injected core of the `gobby-tasks-ops:start_expansion_run`
    MCP tool. The MCP closure simply resolves the dependencies from
    RegistryContext and forwards to this impl; the dispatcher (§1.9)
    constructs the dependencies from the daemon services it already holds
    and calls the impl directly. Returns the persisted `ExpansionRun`
    (with `.id` populated) even on compile failure — the caller persists
    the id so `rule_validate_expansion` and expansion-qa (§2.9) can
    observe the failed run and reject."""
```

Dispatcher dependency wiring (per §1.9 / §1.10 R7.F-dispatcher-services): the dispatcher reads every dependency from the `DispatcherServices` instance threaded through `run_tick(services, ...)`. No private global helpers (`_task_manager(db)`, `_llm_service()`, etc.) — all five dependencies (`task_manager`, `llm_service`, `config`, `completion_registry`, `triggering_session_id`) come from `services` fields populated in `runner_init.py` from live `runner` attributes. The MCP closure path stays untouched — this is purely an in-process additional caller.

The dispatcher captures `run.id` and writes it into `task_artifacts.expansion_run_id` (§1.9). Compile failures are recoverable: `rule_validate_expansion` waits for the run to reach a terminal state (completed OR failed); on failed, expansion-qa picks it up and rejects via `mark_task_review_rejected(lifecycle=expanding)`, which clears `expansion_run_id` and increments `expansion_attempts` (§1.8 R4.F1 extension).

**Acceptance:**

- 2.8b.1 — `start_expansion_run_impl` exported from `src/gobby/mcp_proxy/tools/tasks/_expansion.py` with the dependency-injected signature above. The MCP closure inside `RegistryContext` resolves its dependencies from context and forwards to this impl; the dispatcher (§1.9) constructs the dependencies from the daemon registry and calls the impl directly. file: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`. test: `tests/tasks/test_start_expansion_run_impl.py` covers (a) the impl is importable and accepts the documented kwargs, (b) the MCP closure forwards correctly without re-implementing logic, (c) the impl returns the `ExpansionRun` with a populated `.id` even on compile failure, (d) the dispatcher path constructs the dependencies and calls the impl without going through MCP transport.
- 2.8b.2 — Dispatcher dependency wiring: §1.10 constructs `DispatcherServices` once at daemon boot from live `runner` attributes (`runner.task_manager`, `runner.llm_service`, `runner.config`, `runner.completion_registry`, `runner.dispatcher_session_id`); §1.9's `_dispatch(StartExpansionRun)` arm calls `start_expansion_run_impl(task_manager=services.task_manager, llm_service=services.llm_service, config=services.config, completion_registry=services.completion_registry, triggering_session_id=services.triggering_session_id, ...)`. No private global helpers — every dependency is a `services.<field>` access. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_dispatcher_expansion_dependencies.py` mocks `DispatcherServices` and asserts each dependency is forwarded to the impl exactly once per dispatch and that no module-level helper functions like `_task_manager` / `_llm_service` exist in `src/gobby/dispatch/dispatcher.py`.

### 2.8c Split `expansion_service.py` monolith [category: refactor] (depends: 2.8, 2.8b)

`kind: deliverable`

Target: `src/gobby/tasks/expansion_service.py` (currently 1,495 lines — exceeds the project's 1,000-line monolith ceiling per `CLAUDE.md` Guiding Principle #2). Split into focused modules under `src/gobby/tasks/expansion/`.

This deliverable fires after the §2.8 / §2.8b additions land so the split happens with the final responsibility surface visible (TDD sandwich shape, agent selection, lifecycle init, in-process impl export). Doing the split before §2.8 would invite a second split round once §2.8's additions push the total back over 1,000 lines.

**Suggested partition** (final shape decided during the refactor; this is the floor, not the ceiling):

- `src/gobby/tasks/expansion/service.py` — public `ExpansionService` facade and orchestration glue (≤ 400 lines).
- `src/gobby/tasks/expansion/tree_shape.py` — stage-driven tree-shape logic (`_skipped_stages`-aware leaf generation; the §2.8.1 work).
- `src/gobby/tasks/expansion/agent_selection.py` — `assigned_agent` selection per leaf, `expansion-agent-selection` skill consumption (the §2.8.2 work plus the agent-selection heuristics).
- `src/gobby/tasks/expansion/lifecycle_init.py` — leaf `lifecycle=in_development` initialization (the §2.8.4 work).
- `src/gobby/tasks/expansion/runs.py` — expansion-run lifecycle: `LocalExpansionRunManager.create` integration, compile kickoff, status transitions (the §2.8b path; pairs with the impl export there).
- `src/gobby/tasks/expansion/__init__.py` — re-exports the public surface so existing import paths (`from gobby.tasks.expansion_service import ...`) continue to work for one release via a thin shim, then are deprecated.

Backward-compat: keep `src/gobby/tasks/expansion_service.py` as a re-export shim during the transition. Any callers outside this epic continue to import from the old path; new callers use the new structure.

**Acceptance:**

- 2.8c.1 — `src/gobby/tasks/expansion/` package created with the modules listed above; every file is under 1,000 lines (≤ 800 lines preferred). file: `src/gobby/tasks/expansion/service.py`. file: `src/gobby/tasks/expansion/tree_shape.py`. file: `src/gobby/tasks/expansion/agent_selection.py`. file: `src/gobby/tasks/expansion/lifecycle_init.py`. file: `src/gobby/tasks/expansion/runs.py`. file: `src/gobby/tasks/expansion/__init__.py`. test: `tests/tasks/test_expansion_module_structure.py` asserts the line-count ceiling per file and the public-surface re-exports.
- 2.8c.2 — `src/gobby/tasks/expansion_service.py` reduced to a re-export shim that imports from the new package. Existing import paths continue to work; `wc -l` on the shim is under 50 lines. behavior: every existing import site (search via `gcode search` for `from gobby.tasks.expansion_service`) keeps working without source-side changes. test: covered indirectly by the existing test suite continuing to pass.
- 2.8c.3 — Internal imports within the codebase migrate to the new module paths. The shim is documented as deprecated for removal in a future release. file: existing call sites under `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/mcp_proxy/tools/tasks/`. test: `gcode search "from gobby.tasks.expansion_service"` returns only the shim itself (and tests asserting the shim).
- 2.8c.4 — Test suite passes after the split: the existing `tests/tasks/test_expansion_*.py` files continue to pass against the new module layout. test: `uv run pytest tests/tasks/test_expansion_*.py`.

### 2.9 Expansion-QA transition contract [category: config] (depends: 1.8)

`kind: deliverable`

> **Status: partial** — plan-coverage and review wiring shipped via commit `4ee54dc5` at `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`; the `assigned_agent`, `category: planning`, and test-infrastructure-only `[category: test]` checks named in the acceptance items are pending.

Target: `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`

R3.F3 fix + R4.F1/F3/F4 extensions. Today's `expansion-qa` validates the expansion run but has no owned transition contract on the parent epic — the plan relied on it without wiring the call. Update the YAML to:

1. **On validation success** (all `### N.N` sections present as subtasks; every automated-category leaf has `assigned_agent`; no `category: planning` leaves; `[category: test]` leaves are infrastructure not authored cases):
   - `mark_task_review_approved(parent_task_id)` — which, per §1.8, advances `lifecycle: expanding → in_development` and resets status to `open`. The `task_artifacts.expansion_run_id` and `expansion_attempts` fields are NOT cleared on approval (kept as audit trail of which run produced the approved tree).
2. **On validation failure or failed expansion run** — call `mark_task_review_rejected(parent_task_id, rejection_notes=<findings>)`. Per §1.8 (R4.F1 extension), this leaves the epic at `lifecycle=expanding, status=open`, **clears `task_artifacts.expansion_run_id`** (so `rule_start_expansion` can re-fire on the next tick), and **increments `task_artifacts.expansion_attempts`**. After `MAX_EXPANSION_ATTEMPTS` (resolved via `_resolve_retry_cap` per §1.7/§2.19, default 3), `rule_start_expansion` either escalates (non-yolo) or force-advances with an audit marker (yolo).

   Validation failures that trigger rejection:
   - **Failed expansion run** (R7.F-failed-run): `expansion-qa` is dispatched whenever the expansion run reaches a terminal state (`completed` OR `failed`, per `rule_validate_expansion`'s `_expansion_run_terminal` predicate, §1.7). On a `failed` run, expansion-qa reads the failure details from the run record and rejects with rejection_notes citing the compile error verbatim. This is what makes the failed-run path actually clear the artifact and increment attempts — the prior `_expansion_run_completed`-only predicate stranded failed runs.
   - **Generic**: missing `### N.N` sections, malformed subtask shapes, mismatched parent linkage.
   - **R4.F3** Missing `assigned_agent` on any automated-category leaf (`code | config | docs | test`). Finding cites the specific leaf: "agent selection pass did not populate assigned_agent for <leaf ref>."
   - **R4.F3** Any leaf with `category: planning`. Finding cites the leaf: "planning category not permitted on leaves; promote to a separate epic or change category."
   - **R4.F4** Any `[category: test]` leaf whose description does not clearly identify test infrastructure (fixtures, helpers, conftest, harness modules). Authored test cases belong inside `[category: code]` leaves' TDD sandwiches. Finding cites the leaf: "test leaf appears to author test cases rather than infrastructure; relocate to the affected code leaf's TDD step."

Unblock `mark_task_review_approved` and `mark_task_review_rejected` in the agent's allowlist for the validation step. Existing task-transitions skill gates (autonomous-only for `mark_*`) still apply.

**Acceptance:**

- 2.9.1 — Expansion-QA transition contract is implemented according to this section. file: `src/gobby/install/shared/workflows/agents/expansion-qa.yaml`.

### 2.10 Merge agent — lifecycle integration [category: config] (depends: 1.8)

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/merge.yaml`

R4.F7 fix. The shipped `merge.yaml` is a pure merge-tool runner: it runs `git merge` and `kill_agent`, but never calls `mark_task_review_*` or writes back to task lifecycle. Without an explicit transition-writing step, `rule_merging` dispatches the agent and the epic stays at `lifecycle=merging` forever. This task makes `merge.yaml` a faithful citizen of the dispatcher's lifecycle state machine.

Keep the existing tool-driven contract that the shipped `merge.yaml` already implements (R6.F3 / R7.F3 fix). Do NOT switch to raw `git` shell calls — `merge.yaml` forbids Bash on purpose, and the existing tools handle everything: `gobby-worktrees:merge_worktree` (with `gobby-merge:merge_*` AI resolution for worktree conflicts), `gobby-clones:sync_clone` + `gobby-clones:merge_clone`. The fix is purely about (a) wiring the dispatcher to spawn the merge agent with the right session variables, (b) extending the agent's finalize step to write task lifecycle, and (c) defining the post-merge cleanup contract using the real tool names.

**Dispatcher side** (recap, no new pseudo-APIs):

- `rule_merging` already emits `SpawnAgent(agent="merge", ...)`. The `merge_runner` prompt builder (§1.6 PROMPT_BUILDERS) reads `task_artifacts` via `gobby-tasks-ops:get_artifacts` (§1.1d) and passes `worktree_id` OR `clone_id` (per the §1.1b CHECK XOR) and `target_branch` as `initial_variables`. The shipped `merge.yaml` already branches on which session var is present.

**`merge.yaml` finalize-step extensions** (the only YAML changes). R7.F-cleanup-ordering: cleanup runs BEFORE terminal approval, so a cleanup failure does not strand the epic at terminal lifecycle with orphaned artifacts. Approval is the LAST step in the success path.

Sequence on a clean merge:

1. **Worktree path**: `gobby-worktrees:mark_worktree_merged(worktree_id)` (registry write). Then `gobby-worktrees:delete_worktree(worktree_id)` (filesystem teardown). Then `gobby-tasks-ops:clear_isolation_pair(epic_task_id, "worktree")` (§1.1d, atomic artifact-pair clear).
   **Clone path**: if `BuildConfig.cleanup_clones_on_merge` is true (§3.1), `gobby-clones:delete_clone(clone_id)`, then `gobby-tasks-ops:clear_isolation_pair(epic_task_id, "clone")`. Otherwise the clone stays on disk and the artifact row is preserved (operator opted into manual cleanup).
2. **Cleanup-failure recovery — non-yolo**: if any of the above tools fails on a non-yolo merge, the agent calls `escalate_task(task_id=epic_task_id, reason="needs_human:merge_cleanup_failed:<failing_step>:<details>")`. The epic stays at `lifecycle=merging, status=escalated` so a human can fix the cleanup state and call `de_escalate_task(task_id, target_status='open', lifecycle=Lifecycle.merging, reason='cleanup recovered')`, which routes the task back through `rule_merging` for a re-dispatch where the agent confirms cleanup state and proceeds to terminal approval. Lifecycle stays non-terminal and artifacts are explicitly accounted for — never orphaned silently.

   **Cleanup-failure recovery — yolo** (R7.F-yolo-cleanup): the constraints invariant is that yolo never escalates. On a yolo merge with cleanup failure, the agent does NOT call `escalate_task`; instead it (a) `gobby-tasks-ops:append_description_section(epic_task_id, heading="Yolo Fallbacks", body="<timestamp>: cleanup failed at <step> (<details>); <worktree|clone> preserved at <path>; lifecycle force-advanced to merged")`, then (b) `gobby-tasks:advance_lifecycle(task_id=epic_task_id, to=Lifecycle.merged, reason="yolo: merge cleanup failed; force-advanced with artifacts preserved", by_actor="merge")`. The artifact pair is left in place for human inspection. The downstream `rule_cascade_close_on_merge` and `rule_archive_plan_on_merge` still fire on the `merged` transition.
3. **Terminal approval (LAST)**: only after step 1 completes successfully, the agent calls `mark_task_review_approved(task_id=epic_task_id, approval_notes="merge completed; cleanup confirmed")`. §1.8 advances `lifecycle: merging → merged, status: closed`. The §1.8.6b stage-advance gate at `lifecycle=merging` requires merge-clean + cleanup-clean state — the gate fires after cleanup so a precondition failure here is the correct signal.

- **Merge SHA capture is deferred**: neither `merge_worktree` nor `merge_clone` currently returns `merge_commit_sha` in its response (verified via current code). Capturing it requires extending those tools, which is real implementation work; deferred to **#12728** alongside PR-creation. The `task_artifacts.merge_commit_sha` column is reserved for that future write but is left NULL on every merge in this epic. No raw `git` calls anywhere — the agent's tool surface forbids Bash.

**Failure paths** (rejection contract — `lifecycle=merging` rejection case in §1.8 R6.F4):

1. **Clean merge** — described above.
2. **Conflict — non-yolo** (worktree only; clone path has no AI resolution per shipped flow): the existing AI flow (`gobby-merge:merge_start` → `merge_status` → `merge_resolve` → `merge_apply`) attempts resolution. If resolution succeeds → success path. If resolution fails (`merge_abort` is called by the agent), the agent calls `escalate_task(task_id=epic_task_id, reason="needs_human: merge conflict on <target_branch>; resolve manually in the worktree, then call de_escalate_task(task_id, target_status='open', lifecycle=Lifecycle.merging, reason='human resolved conflict; ready for merge agent retry')")`. R7.F-non-yolo-recovery: de-escalating to `lifecycle=merging, status=open` (NOT directly to `merged`) routes the task back through `rule_merging`, which re-dispatches the merge agent. The agent's success path then runs the normal cleanup — `mark_worktree_merged`, `delete_worktree`, `clear_isolation_pair` — and writes the terminal lifecycle. De-escalating directly to `lifecycle=merged` was the prior contract but skipped artifact cleanup and the merge-success write-back, leaving worktrees orphaned and the lifecycle event audit incomplete. For clone conflicts, the same escalate path is taken directly (no AI resolution attempt — clone has no `gobby-merge:*` integration); recovery follows the same `de_escalate_task(... lifecycle=Lifecycle.merging ...)` pattern so `rule_merging` can re-dispatch with the now-resolvable clone state.
3. **Conflict — yolo** (worktree): try AI resolution as in (2); on success, take the success path. On failure, increment a `merge-attempts:N` label on the epic via the existing `add_label` / `update_task` (labels) tools (cap resolved from `max_merge_attempts` session variable per R7.F-merge-cap-wired — `rule_merging` populates the var from `_resolve_retry_cap(task, "max_merge_attempts", MAX_MERGE_ATTEMPTS_DEFAULT)`, default 3, overridable per §2.19.3). Call `mark_task_review_rejected(task_id=epic_task_id, rejection_notes="yolo conflict resolution attempt failed: <details>")` — see §1.8 R6.F4 for the `lifecycle=merging` rejection contract — then `kill_agent`. `rule_merging` re-dispatches on the next tick. (Clone-with-yolo: same retry pattern, but each attempt is a fresh `sync_clone` + `merge_clone`; no AI step.)
4. **Yolo conflict — retries exhausted** (`merge-attempts:N >= cap`): preserve the R3.U1 "yolo never escalates" invariant via the documented force-advance fallback. The fallback uses the explicit `advance_lifecycle` tool, NOT `mark_task_review_approved`, because the stage-advance gate at `lifecycle=merging` requires merge-clean state (per §1.8.6) — using `mark_task_review_approved` here would deadlock the fallback exactly when it's needed. Sequence: (a) `gobby-tasks-ops:append_description_section(epic_task_id, heading="Yolo Fallbacks", body="<timestamp>: merge attempts exhausted (<N>); <worktree|clone> preserved at <path>; lifecycle force-advanced to merged without merge")` (§1.1d). (b) `gobby-tasks:advance_lifecycle(task_id=epic_task_id, to=Lifecycle.merged, reason="yolo: merge attempts exhausted; force-advanced without merge", by_actor="merge")` — `advance_lifecycle` (§1.8) sets `status=closed` automatically when the new lifecycle is `merged` (terminal), so the epic reaches `(merged, closed)` in one call. The artifact pair is **NOT cleared** on this path — cleanup is skipped so a human can inspect (the artifact row stays populated with `worktree_path`/`worktree_id` or `clone_path`/`clone_id`). Documented exception under R3.U1. The downstream `rule_cascade_close_on_merge` and `rule_archive_plan_on_merge` (§1.7) still fire as expected on the `merged` transition.

**Allowlist additions** to merge.yaml's existing `allowed_mcp_tools`:

- `gobby-tasks:mark_task_review_approved`
- `gobby-tasks:mark_task_review_rejected`
- `gobby-tasks:escalate_task`
- `gobby-tasks:advance_lifecycle` (yolo cap-exhausted force-advance; bypasses stage-advance review gates per §1.8)
- `gobby-tasks:add_label` (for `merge-attempts:N` increments)
- `gobby-tasks-ops:get_artifacts` (initial read of `worktree_id`/`clone_id`/`target_branch` if not already in session vars)
- `gobby-tasks-ops:clear_isolation_pair` (§1.1d — clean up artifact pair atomically on success)
- `gobby-tasks-ops:append_description_section` (§1.1d — yolo-fallback audit marker)

The existing tool list (per shipped merge.yaml) already includes `gobby-worktrees:merge_worktree`, `gobby-clones:sync_clone`, `gobby-clones:merge_clone`, `gobby-merge:merge_*`, `gobby-agents:kill_agent` — those stay. Add `gobby-worktrees:mark_worktree_merged` and `gobby-worktrees:delete_worktree` for the cleanup step. Existing task-transitions skill gates (autonomous-only for `mark_*`) still apply.

Real PR-creation, merge-SHA capture, and AI-driven conflict resolution for clones remain in **#12728**. This task ships only the lifecycle handshake on top of the existing tool surface; no new git-driving code, no new merge mechanics.

**Acceptance:**

- 2.10.1 — Merge agent receives worktree_id or clone_id plus target_branch through dispatcher initial variables and reads artifacts when needed. file: `src/gobby/install/shared/workflows/agents/merge.yaml`.
- 2.10.2 — Clean merge success path runs cleanup BEFORE terminal approval (R7.F-cleanup-ordering). Sequence: (a) `mark_worktree_merged` / `delete_worktree` / `clear_isolation_pair` (worktree path) OR `delete_clone` / `clear_isolation_pair` (clone path), then (b) `mark_task_review_approved`. Cleanup-failure handling splits by yolo (R7.F-yolo-cleanup): non-yolo calls `escalate_task(reason="needs_human:merge_cleanup_failed:<step>:<details>")` and the epic stays at `lifecycle=merging, status=escalated`; yolo calls `[append_description_section("Yolo Fallbacks", ...), advance_lifecycle(to=Lifecycle.merged, reason="yolo: merge cleanup failed; force-advanced with artifacts preserved", by_actor="merge")]` so it never escalates per the top-level yolo invariant — artifacts preserved for human inspection. Approval is the LAST step in the non-yolo clean-success path; the yolo cleanup-failure path advances directly via `advance_lifecycle` and never calls `mark_task_review_approved`. file: `src/gobby/install/shared/workflows/agents/merge.yaml`. test: `tests/agents/test_merge_cleanup_ordering.py` covers (a) clean path executes cleanup → approval in order on non-yolo, (b) non-yolo cleanup-failure escalates with `needs_human:merge_cleanup_failed:` reason and lifecycle stays at `merging`, (c) **yolo cleanup-failure NEVER calls `escalate_task`** — instead force-advances to `merged` via `advance_lifecycle` with audit marker, (d) yolo and non-yolo both preserve uncleared artifacts on cleanup failure for human inspection, (e) re-dispatch after `de_escalate_task` retries cleanup on the non-yolo path and lands at terminal `merged` only when cleanup succeeds.
- 2.10.3 — Non-yolo conflicts escalate with needs_human instructions while preserving lifecycle state for recovery. behavior: `non-yolo merge conflict path in §2.10`.
- 2.10.4 — Yolo conflicts retry through `mark_task_review_rejected` (lifecycle stays `merging`, status resets to `open`, `merge-attempts:N` label increments) until `merge-attempts:N >= cap`. On cap exhaustion, the agent calls `gobby-tasks-ops:append_description_section(heading="Yolo Fallbacks", body=...)` followed by `gobby-tasks:advance_lifecycle(task_id, to=Lifecycle.merged, reason="yolo: merge attempts exhausted; force-advanced without merge", by_actor="merge")` — `advance_lifecycle` (not `mark_task_review_approved`) because the stage-advance gate at `lifecycle=merging` requires merge-clean state and would deadlock the fallback. The artifact pair is preserved (no `clear_isolation_pair` call). file: `src/gobby/install/shared/workflows/agents/merge.yaml`. test: `tests/agents/test_merge_yolo_fallback.py` covers (a) under-cap retries via mark_task_review_rejected, (b) cap-exhausted force-advance via advance_lifecycle, (c) artifact pair preserved on force-advance, (d) downstream rule_cascade_close_on_merge and rule_archive_plan_on_merge fire correctly on the resulting `lifecycle=merged` state.

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
- **Escalate for rescope** → `escalate_task(epic, reason="needs_human:rescope_required:<details>")` OR `reason="needs_human:requirements_unclear:<details>"`. §1.8 third-path: epic flips to `status=escalated, lifecycle=holistic_review` (lifecycle unchanged); user resumes via `de_escalate_task(epic, target_status=..., lifecycle=...)` after revising the plan or accepting the rework scope. Used when implementation is fine but the spec is wrong.

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

**`mark_task_*` validation behavior** (per §1.8.6 — verdict-AND-stage-aware gates):

- **leaf-close set**: `mark_task_review_approved` on a leaf at `lifecycle=in_development` (qa-reviewer caller) runs the full close-equivalent gates (commit-attached, validation_criteria pass, errors_resolved, memory_review_completed). `skip_validation` rejected. This is the only approval path that ships code on the approved task.
- **stage-advance set**: `mark_task_review_approved` on a task at `lifecycle ∈ {plan_review, test_arch, expanding, holistic_review, merging}` runs stage-specific output gates only (manifest emitted, test architecture appended, expansion run completed, leaves terminal-or-holistic, merge clean). It does NOT require commit-attached or validation_criteria on the approving task — these stages advance lifecycle, they don't ship code on the approving task.
- **light set**: `mark_task_review_rejected` and `escalate_task` at any lifecycle run only valid claim/session + `rejection_notes`/`reason` non-empty. Rejection is the path for reporting that validation failed or commits are missing; requiring those gates would deadlock the read-only reviewer.
- Build-mode session variables (`task_claimed`, `errors_resolved`, etc.) are set by the agent's workflow steps for the leaf-close path the same way they are for interactive sessions. The stage-advance and light sets do not require those variables; they look at the stage's output instead.

**Acceptance:**

- 2.14a.1 — `BUILD_AGENT_NAMES` constant defined in `src/gobby/agents/roles.py` enumerating every autonomous build agent: `{"qa-reviewer", "holistic-reviewer", "expansion-qa", "merge", "test-architect", "plan-adversary", "planner", "backend-developer", "frontend-developer", ...}` (full list per the audit). file: `src/gobby/agents/roles.py`. test: `tests/agents/test_roles.py` covers the contents and freezing.
- 2.14a.2 — Audit document classifies every rule that fires on agent edit/stop/transition events per the table above. file: `src/gobby/install/shared/rules/AGENT_SCOPE_AUDIT.md`.
- 2.14a.3 — `require-task-close`, `require-clean-tree-before-status`, and other interactive-only rules carry a session-role / agent-name predicate so they no longer block build agents. test: `tests/rules/test_build_agent_scope.py` asserts each interactive-only rule does NOT fire when the active session belongs to a `BUILD_AGENT_NAMES` agent.
- 2.14a.4 — Replacement rule `require-mark-task-terminal` blocks autonomous-build-agent stop when no `mark_task_*` or `escalate_task` has been called for the agent's claimed task. file: `src/gobby/install/shared/rules/require_mark_task_terminal.yaml`. test: `tests/rules/test_require_mark_task_terminal.py` covers each build agent and asserts stop is blocked until the terminal call lands.
- 2.14a.5 — Verdict-AND-stage-aware gates per §1.8.6. Three gate sets: **leaf-close** (full close-equivalent gates: commit-linked, validation_criteria pass, errors_resolved, memory_review_completed; `skip_validation` stripped) for qa-reviewer's leaf approval; **stage-advance** (stage-specific output gates only — manifest emitted / test architecture appended / expansion completed / leaves terminal-or-holistic / merge clean) for plan-adversary, test-architect, expansion-qa, holistic-reviewer, and merge stage approvals; **light** (valid claim/session + actionable `rejection_notes`/`reason`) for any rejection or escalation. The rejection path MUST succeed when commits are missing or validation_criteria fails — that is the state the reviewer is reporting. The stage-advance set MUST succeed without commit-attached on the approving task — those stages don't ship code on the task they approve. test: `tests/storage/test_mark_task_review_validation.py` (cross-references §1.8.6).

### 2.14b Deprecation pattern for retired agents and pipelines [category: config]

`kind: deliverable`

Target: new directories `src/gobby/install/shared/workflows/agents/deprecated/` AND `src/gobby/install/shared/workflows/pipelines/deprecated/`; bundled-sync logic in `src/gobby/workflows/loader.py` (extend for both definition kinds); `CLAUDE.md` (update Plan-Coverage / orchestration section to document the new pattern, replacing the tombstone-flag prose).

CLAUDE.md currently describes a tombstone pattern for retired orchestration templates: keep the file in place with `enabled: false, deprecated: true` so bundled-sync stabilizes the existing DB row. The user's preferred pattern going forward is cleaner: **deprecated definitions move into a `deprecated/` subdirectory AND bundled-sync removes their DB rows on next startup**. Filesystem location signals deprecation; the DB stays clean.

R7.F-pipeline-class fix: the retired orchestrators are **pipeline definitions**, not agents. The actual files in the repo today are at `src/gobby/install/shared/workflows/pipelines/orchestrator.yaml`, `pipelines/front-half-orchestrator.yaml`, `pipelines/dev-orchestrator.yaml`, `pipelines/delivery-orchestrator.yaml`, and `pipelines/conductor.yaml`. The agent `agents/conductor.yaml` is a separate, distinct file (an agent definition that the conductor pipeline used to spawn). Both kinds need first-class deprecation handling in this epic — `agents/deprecated/` for the conductor agent + qa-dev + old qa-reviewer; `pipelines/deprecated/` for the five retired pipeline orchestrators.

**Pattern** (applies identically to agents and pipelines):

- Move the file from `…/<kind>/<name>.yaml` to `…/<kind>/deprecated/<name>.yaml`.
- The yaml content keeps as-is (no need to flip `enabled: false`); the path signals deprecation.
- Bundled-sync on startup walks `<kind>/deprecated/` for each kind and ensures any DB row matching a deprecated definition's name (and matching kind) is removed. Same shape can extend to `rules/deprecated/` and `workflows/deprecated/` for future deprecations; only the agents and pipelines variants are required for this epic.

**Acceptance:**

- 2.14b.1 — New directories `src/gobby/install/shared/workflows/agents/deprecated/` AND `src/gobby/install/shared/workflows/pipelines/deprecated/` exist; bundled-sync recognizes both subdirectories at startup. file: `src/gobby/install/shared/workflows/agents/deprecated/.gitkeep`. file: `src/gobby/install/shared/workflows/pipelines/deprecated/.gitkeep`. behavior: walking either directory does not error when empty.
- 2.14b.2 — Bundled-sync removes DB rows for agent definitions found in `agents/deprecated/`. file: `src/gobby/workflows/loader.py`. behavior: idempotent — re-running on already-removed rows is a no-op. test: `tests/workflows/test_loader_deprecated_agents.py`.
- 2.14b.3 — Bundled-sync removes DB rows for pipeline definitions found in `pipelines/deprecated/`. file: `src/gobby/workflows/loader.py`. behavior: idempotent. test: `tests/workflows/test_loader_deprecated_pipelines.py`.
- 2.14b.4 — Migrate the five retired pipeline orchestrators from `pipelines/` to `pipelines/deprecated/`: `orchestrator.yaml`, `front-half-orchestrator.yaml`, `dev-orchestrator.yaml`, `delivery-orchestrator.yaml`, `conductor.yaml` (the pipeline). Remove the `enabled: false, deprecated: true` flags from their yaml content; the path now signals deprecation. file: each filename at the new path under `pipelines/deprecated/`. test: a regression test asserts that `list_workflow_definitions(kind="pipeline", name=<each>)` returns no row after sync.
- 2.14b.5 — Migrate the conductor agent (`agents/conductor.yaml`, separate from the pipeline of the same name) to `agents/deprecated/conductor.yaml`. file: `src/gobby/install/shared/workflows/agents/deprecated/conductor.yaml`. test: regression test asserts the agent definition row is removed after sync.
- 2.14b.6 — `qa-dev.yaml` and old `qa-reviewer.yaml` move to `agents/deprecated/` as part of §2.11.1 / §2.11.2 (this section provides the pattern; §2.11 applies it). behavior: tested as part of §2.11.
- 2.14b.7 — `CLAUDE.md` documents the new `deprecated/` directory pattern (covering both agents and pipelines), replacing the tombstone-flag description for orchestrators. file: `CLAUDE.md`.

### 2.15 DB-backed plan state + `gobby-plans` MCP/CLI [category: code]

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
- 2.15.7 — `docs/contracts/plan-coverage.md` Plan Index section is replaced (R7.F-contract-doc-sync). Removed: the entire "Plan index" subsection that documented `.gobby/plans/index.yaml` as authoritative + entry shape; the `merged` value from the documented `state` enum; any prose treating `index.yaml` as the source of truth. Added: a "Plan Storage" subsection naming the `plans` table (§2.15 schema) as the source of truth, the `gobby-plans` MCP server + `gobby plans` CLI as the read/write surfaces, and the `state` enum reduced to `active | archived`. file: `docs/contracts/plan-coverage.md`. test: `tests/docs/test_plan_coverage_contract_sync.py` greps the file for `index.yaml`, `merged`-as-state, and `legacy`-as-plan_kind, asserting all three are absent post-revision.

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
- 2.16.6 — `docs/contracts/plan-coverage.md` waiver/grandfathered section (R7.F-contract-doc-sync): remove the entire "Grandfathered plans" subsection (the `.grandfathered` mechanism), remove all references to `.legacy-classification.yaml`, and remove `legacy` from the `plan_kind` enum documentation. The contract doc must end up with only `implementation | strategy` as `plan_kind` values and no waiver mechanism — if a future need emerges it gets DB-backed per §2.15. file: `docs/contracts/plan-coverage.md`. test: `tests/docs/test_plan_coverage_contract_sync.py::test_no_grandfathered_or_legacy` greps the file for `.grandfathered`, `.legacy-classification`, and `plan_kind: legacy`, asserting all three are absent post-revision.

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

One row per retry cap from the table above; each item below covers all five axes — BuildConfig field, BuildOptions field, CLI flag, persistence into `task_artifacts`, read path through `_resolve_retry_cap` per §1.7.

**Acceptance:**

- 2.19.1 — Cap `max_expansion_attempts` (default 3): `BuildConfig.max_expansion_attempts: int = 3`; `BuildOptions.max_expansion_attempts`; CLI flag `--max-expansion-attempts N`; persisted to `task_artifacts` at `gobby build` dispatch time; `rule_start_expansion` (§1.7) reads via `_resolve_retry_cap(task, "max_expansion_attempts", MAX_EXPANSION_ATTEMPTS_DEFAULT)`. file: `src/gobby/config/build.py`, `src/gobby/build/service.py`, `src/gobby/cli/build.py`. test: `tests/dispatch/test_rules_retry_caps.py::test_max_expansion_attempts` covers default + override + persistence + read path.
- 2.19.2 — Cap `max_qa_rounds` (default 5): `BuildConfig.max_qa_rounds: int = 5`; `BuildOptions.max_qa_rounds`; CLI flag `--max-qa-rounds N`; persisted to `task_artifacts` at dispatch; `rule_qa` (§1.7) reads via `_resolve_retry_cap(task, "max_qa_rounds", MAX_QA_ROUNDS_DEFAULT)`. file: same paths as 2.19.1. test: `tests/dispatch/test_rules_retry_caps.py::test_max_qa_rounds`.
- 2.19.3 — Cap `max_merge_attempts` (default 3): `BuildConfig.max_merge_attempts: int = 3`; `BuildOptions.max_merge_attempts`; CLI flag `--max-merge-attempts N`; persisted to `task_artifacts` at dispatch; merge agent (§2.10) reads via `_resolve_retry_cap(task, "max_merge_attempts", MAX_MERGE_ATTEMPTS_DEFAULT)` and consumes the `merge-attempts:N` label as the live counter. The `MAX_MERGE_ATTEMPTS_DEFAULT` constant is added to `src/gobby/dispatch/rules.py` alongside the other defaults. file: same paths as 2.19.1, plus `src/gobby/install/shared/workflows/agents/merge.yaml` reads the resolved cap from session variables set at spawn time. test: `tests/dispatch/test_rules_retry_caps.py::test_max_merge_attempts`.
- 2.19.4 — Cap `max_holistic_rounds` (default 3): `BuildConfig.max_holistic_rounds: int = 3`; `BuildOptions.max_holistic_rounds`; CLI flag `--max-holistic-rounds N`; persisted to `task_artifacts` at dispatch; holistic-reviewer step contract (§2.12) reads via `_resolve_retry_cap(task, "max_holistic_rounds", MAX_HOLISTIC_ROUNDS_DEFAULT)`. The `MAX_HOLISTIC_ROUNDS_DEFAULT` constant is added to `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_rules_retry_caps.py::test_max_holistic_rounds`.
- 2.19.5 — Cap `max_review_rounds` (default 3): `BuildConfig.max_review_rounds: int = 3` (consolidate with the existing field if already present from `/gobby plan max_rounds`); `BuildOptions.max_review_rounds`; CLI flag `--max-review-rounds N` (already exists); persisted to `task_artifacts` at dispatch; `rule_plan_adversary` (§1.7) reads via `_resolve_retry_cap(task, "max_review_rounds", MAX_REVIEW_ROUNDS_DEFAULT)`. The `MAX_REVIEW_ROUNDS_DEFAULT` constant is added (or aligned with the existing one). test: `tests/dispatch/test_rules_retry_caps.py::test_max_review_rounds`.
- 2.19.6 — Resolution order across all five caps: CLI flag > profile-resolved value > BuildConfig default > module-level `_DEFAULT` constant. `BuildOptions` resolution applies this order; persistence writes the resolved value into `task_artifacts`; rules read via `_resolve_retry_cap` so subsequent ticks honor the dispatch-time value even if the config store changes mid-build. file: `src/gobby/build/service.py`. test: `tests/build/test_options_resolution.py` covers the resolution-order matrix for each cap.
- 2.19.7 — `BuildResult` includes the resolved retry caps so the operator can see what the build is using. behavior: status output / JSON response shows all five resolved caps. file: `src/gobby/build/service.py`. test: `tests/build/test_result_shape.py::test_retry_caps_in_result`.

### 2.21 Plan-Coverage Contract: `## Task Manifest` section requirement [category: docs]

`kind: deliverable`

Target: `docs/contracts/plan-coverage.md` (extend); `src/gobby/install/shared/skills/plan-draft/SKILL.md` (extend); `src/gobby/plans/parser.py` (parser updates); `CLAUDE.md` (Plan-Coverage Contract section update).

**Why**: Today, expansion infers the task list from `kind: deliverable` sections. That's section-driven and implicit — the plan author writes prose; the system interprets. The contract makes the task list **explicit** via a `## Task Manifest` section at the end of every implementation plan. The manifest is the single source of truth for what tasks expansion creates; sections become human-readable narrative documenting why each task exists. The manifest is **written by plan-adversary on approval** (§2.22), not by the planner — the same agent that just verified the plan is the one writing the canonical task list. Manifest drift is impossible by construction.

**Section shape** (added to the contract). R7.F-manifest-id: the manifest heading carries an explicit `M1` section ID so it satisfies the canonical regex `^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))`. `M1` matches the alpha-prefix-plus-digit branch (same shape as `P1`, `V1`, `O1` framing headings already in this plan). Without an ID, the post-approval `parse_plan(..., parse_mode="expansion")` self-check (§2.22.4) would mechanically reject the manifest. The adversary writes `## M1 Task Manifest` (NOT `## Task Manifest`) to keep the contract regex-honest.

````markdown
## M1 Task Manifest
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

- New `kind` enum value: `manifest`. At most one `kind: manifest` section per plan; placed at the end. The manifest heading uses the canonical ID `M1` (i.e. `## M1 Task Manifest`) so it satisfies the section-ID regex. The parser treats `kind: manifest` as exempt from the `**Acceptance:**` requirement (manifest entries take the place of acceptance items for this section). Other allowed `kind` values stay: `deliverable | framing | verification | deferred`.
- Manifest entry schema: `title` (str), `category` (enum), `task_type` (enum), `depends_on` (list[str], references plan section IDs), `validation_criteria` (str), `labels` (list[str], must include exactly one `covers:` label per acceptance item in the source section), `assigned_agent` (str), `tdd` (bool — implies the deterministic compiler emits a TEST/IMPL/REF triple), `source_section` (str, references a `kind: deliverable` section ID).

**Parser modes** (resolves the manifest-deadlock between §2.21 and §2.22):

`parse_plan` accepts a `parse_mode` parameter selecting the validation strictness. The mode determines whether the manifest is required and how the deliverable→manifest invariants are applied:

| Mode | Manifest | Used by | Behavior |
|------|----------|---------|----------|
| `parse_mode="draft"` | OPTIONAL | plan-adversary during the review loop; `/gobby plan` Phase 3a; `gobby plan coverage` when run against a draft | Parser passes whether or not `## Task Manifest` is present. If the section is present, schema and 1:1 invariants ARE enforced (a malformed manifest still fails). If absent, parser succeeds — adversary review must not deadlock on a not-yet-written manifest. |
| `parse_mode="expansion"` | REQUIRED | `gobby expand` deterministic compile path; plan-adversary's post-approval self-check (§2.22.4); §2.20 re-expansion gate | Parser fails with `PlanParseError` if `## Task Manifest` is absent or if any deliverable has no manifest entry. This is the strict mode that gates expansion. |
| `parse_mode="strict"` (default) | REQUIRED | callers that want full validation regardless of context | Same strict invariants as `expansion`. Default so existing call sites that omit `parse_mode` keep their current behavior, except the plan-review skill and the adversary's pre-verdict gate, which pass `parse_mode="draft"` explicitly. |

The deadlock is resolved by construction: the adversary reviews in `draft` mode (parser tolerant of missing manifest); on clean review, adversary writes the manifest and self-checks in `expansion` mode (parser strict); on success, mark_task_review_approved fires and downstream `gobby expand` parses in `expansion` mode against the now-manifest-bearing plan. No phase ever needs the manifest before the previous phase has had a chance to write it.

- Parser-enforced invariants (when manifest is present, regardless of mode): every `kind: deliverable` section has exactly one manifest entry referencing it via `source_section`; every `covers:` label resolves to a real acceptance item; no orphan manifest entries (every entry's `source_section` resolves to a real deliverable).

**Acceptance:**

- 2.21.1 — `docs/contracts/plan-coverage.md` documents the `## Task Manifest` section, the entry schema, the parser-enforced invariants, and the adversary-writes-on-approval contract. file: `docs/contracts/plan-coverage.md`.
- 2.21.2 — `plan-draft` SKILL.md is updated: planner authors narrative sections only, never the manifest. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- 2.21.3 — Parser (`src/gobby/plans/parser.py`) accepts a `parse_mode` parameter (`Literal["draft", "expansion", "strict"]`) with default `"strict"`. The parameter signature lands first; per-mode behavior items follow. file: `src/gobby/plans/parser.py`. test: `tests/plans/test_parser_manifest.py::test_parse_mode_signature_accepts_three_values`.
- 2.21.3a — Mode `parse_mode="draft"`: parser tolerates missing `## Task Manifest` section (review loop never deadlocks on a not-yet-written manifest). When the manifest IS present in draft mode, the schema check + deliverable→manifest 1:1 invariant + `covers:` label resolution still run (a malformed draft manifest still fails). The plan-review skill (consumed by plan-adversary) calls `parse_plan(..., parse_mode="draft")`. file: `src/gobby/plans/parser.py`. test: `tests/plans/test_parser_manifest.py::test_draft_tolerates_missing_manifest_but_validates_present_one`.
- 2.21.3b — Mode `parse_mode="expansion"`: parser raises `PlanParseError("missing manifest")` when `## Task Manifest` is absent. When present, all schema and invariant checks run identically to other modes. `gobby expand` and §2.22.4 plan-adversary self-check call `parse_plan(..., parse_mode="expansion")`. file: `src/gobby/plans/parser.py`. test: `tests/plans/test_parser_manifest.py::test_expansion_rejects_missing_manifest`.
- 2.21.3c — Mode `parse_mode="strict"` (default): same strict invariants as `expansion` — manifest required, schema and 1:1 invariant enforced. Default is `strict` so any caller that omits `parse_mode` keeps full validation; the plan-review skill and the post-approval self-check are the only callers that pass an explicit non-strict mode. file: `src/gobby/plans/parser.py`. test: `tests/plans/test_parser_manifest.py::test_strict_default_rejects_missing_manifest`.
- 2.21.3d — Cross-mode invariant: when the manifest IS present, schema-check each entry, enforce the deliverable→manifest-entry 1:1 invariant, resolve every `covers:` label against acceptance items, and verify no orphan manifest entries — regardless of mode. Only the missing-manifest behavior differs by mode. file: `src/gobby/plans/parser.py`. test: `tests/plans/test_parser_manifest.py::test_manifest_invariants_enforced_in_all_modes` covers a malformed manifest rejected in `draft`, `expansion`, and `strict`.
- 2.21.4 — `CLAUDE.md` Plan-Coverage Contract section updated to mention the manifest. file: `CLAUDE.md`.

### 2.21a Manifest-emitter library [category: code] (depends: 2.21)

`kind: deliverable`

Target: `src/gobby/plans/manifest_emitter.py` (new file).

R7.F-manifest-emitter-deliverable fix. Both §1.9's `EmitStubManifest` dispatch arm and §2.22.4's yolo self-check fallback call `gobby.plans.manifest_emitter.emit_stub_manifest(...)`, but no deliverable existed for that module. This deliverable creates the module with a stable public signature so both callers (dispatcher path + plan-adversary agent path) consume the same implementation.

**Public signature**:

```python
# src/gobby/plans/manifest_emitter.py
from pathlib import Path
from typing import Literal

from gobby.plans.parser import parse_plan, PlanKind, PlanParseError

EmitOutcome = Literal["fresh", "replaced_malformed", "noop_existing_valid", "fallback_force_approve"]

def emit_stub_manifest(
    plan_path: str | Path,
    *,
    by_actor: str = "dispatcher",
    plan_kind: PlanKind = PlanKind.implementation,
) -> EmitOutcome:
    """Idempotent manifest emitter for the yolo cap-exhausted fallback path.

    Sequence:
    1. Read the plan file. Parse in `parse_mode="draft"`.
    2. If a valid `## M1 Task Manifest` already exists (parse_mode="expansion"
       passes against the existing manifest), return "noop_existing_valid".
    3. If a malformed manifest exists, replace it with a freshly-synthesized
       one and re-validate. On success return "replaced_malformed".
    4. If no manifest exists, synthesize one from the plan's `kind: deliverable`
       sections and write it. On success return "fresh".
    5. If post-write validation fails (the deliverable schema in the plan is
       malformed beyond the emitter's reach — e.g., missing acceptance items
       on a deliverable section), append a `## Yolo Fallbacks` audit section
       to the plan file documenting the failure mode and return
       "fallback_force_approve". This is the absorbed-failure outcome — the
       function NEVER raises; the yolo invariant requires deterministic
       progress regardless of input quality. Downstream `gobby expand` will
       reject the plan when it parses in `parse_mode="expansion"`, surfacing
       the issue at expansion time where a human can intervene.

    The synthesized manifest:
    - One entry per `kind: deliverable` section.
    - `source_section` set to the section ID.
    - `covers:<plan-id>:<section-id>:<item-id>` labels for every acceptance item.
    - `assigned_agent="backend-developer"` (default; the audit marker captures
      that this was force-generated rather than authored).
    - `tdd=True` for `code | test | refactor` categories; `False` for
      `config | docs | research | planning | manual`.
    - `task_type="feature"` default.
    - `validation_criteria` copied from the deliverable section's first
      acceptance item's artifact reference.
    """
```

The module exports only `emit_stub_manifest` plus the `EmitOutcome` literal; everything else is a private helper.

**Acceptance:**

- 2.21a.1 — `src/gobby/plans/manifest_emitter.py` exposes `emit_stub_manifest(plan_path, *, by_actor, plan_kind) -> EmitOutcome` per the signature above. `EmitOutcome` is a typed `Literal` covering `"fresh" | "replaced_malformed" | "noop_existing_valid" | "fallback_force_approve"`. file: `src/gobby/plans/manifest_emitter.py`. test: `tests/plans/test_manifest_emitter.py::test_signature` covers the import surface and the outcome literal values.
- 2.21a.2 — Fresh emission: a plan with no manifest gets a `## M1 Task Manifest` synthesized from `kind: deliverable` sections, with `source_section` and `covers:` labels per the contract above. The post-write `parse_plan(parse_mode="expansion")` passes. test: `tests/plans/test_manifest_emitter.py::test_fresh_emission` covers a plan with N deliverables → manifest with N entries.
- 2.21a.3 — Idempotent on valid existing manifest: rerunning on a plan that already has a parse_expansion-clean manifest returns `"noop_existing_valid"` and does not modify the file. test: `tests/plans/test_manifest_emitter.py::test_noop_on_valid_existing`.
- 2.21a.4 — Replaces malformed existing manifest: a plan with a `## M1 Task Manifest` that fails `parse_mode="expansion"` (missing entries, broken `covers:` labels, or schema-malformed entries) gets the malformed section replaced with a freshly-synthesized one. Returns `"replaced_malformed"`. test: `tests/plans/test_manifest_emitter.py::test_replace_malformed`.
- 2.21a.5 — Absorbed failure on plan-shape problems: a plan whose `kind: deliverable` sections themselves are malformed beyond the emitter's reach (e.g., missing acceptance items) appends a `## Yolo Fallbacks` audit section and returns `"fallback_force_approve"`. The function NEVER raises — yolo invariant compliance. test: `tests/plans/test_manifest_emitter.py::test_fallback_force_approve_no_raise` constructs a plan with a deliverable section missing its `**Acceptance:**` block and asserts (a) emit_stub_manifest returns "fallback_force_approve", (b) no exception was raised, (c) a Yolo Fallbacks audit section was appended.
- 2.21a.6 — Generated manifest entries default `assigned_agent="backend-developer"`, `tdd` per category (`code|test|refactor` → True, others → False), `task_type="feature"`. test: `tests/plans/test_manifest_emitter.py::test_default_assignment_and_tdd_by_category`.

### 2.22 plan-adversary agent: manifest emission on approval [category: config] (depends: 2.21, 2.21a)

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`; `src/gobby/install/shared/skills/plan-review/SKILL.md`.

**New responsibility**: plan-adversary, in addition to reviewing each round and emitting findings, now writes the `## Task Manifest` YAML in the same call where it approves the plan. The act of writing the manifest forces the adversary to confront ambiguity it might otherwise wave through — if the adversary cannot write a manifest entry for a deliverable, the plan isn't ready.

**Updated workflow per round**:

1. Read plan file + cumulative `## Plan Changelog` (per §2.23 planner additions).
2. Review against contract + intent.
3. **If findings**: emit `## Adversary Findings — Round N` to the planning task description; call `mark_task_review_rejected(round_number=N, rejection_notes=...)`. Do NOT edit the plan file. Planner handles edits between rounds (§2.23).
4. **If clean**: append `## Task Manifest` YAML to the plan file (per §2.21 schema). Run parser self-check on the manifest. If parser fails, fix the manifest in-place and re-self-check; up to 3 retries before escalating with `escalate_task(reason="needs_human:manifest_emission_failure:<details>")`. On success: `mark_task_review_approved(approval_notes="...; manifest emitted with N entries")`.
5. Always exit fresh — no carryover context to next round (next round gets a new instance per §2.23 fresh-context-per-round contract).

**Tool surface adjustments**:

- Add `Edit` and `Write` permission to plan-adversary's tool surface (currently read-only-ish) — it now writes the manifest section. Edit/Write is **scoped to the plan file path only**; writing to any other path fails. Implementation: a per-step path allowlist enforced via the same `denied_bash_substrings` / `denied_tools` mechanism used elsewhere; the allowlist is computed at agent-spawn time from `task_artifacts.plan_file_path`.
- Add `parse_plan` invocation via the parser library (or via a dedicated MCP tool that wraps the parser) so the adversary can self-check its own manifest output before committing.

**Acceptance:**

- 2.22.1 — `plan-adversary.yaml` review step: on approval (no findings), the agent writes the `## M1 Task Manifest` YAML (note the M1 ID — required by the canonical heading regex per §2.21 R7.F-manifest-id) at the end of the plan file before calling `mark_task_review_approved`. file: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test: `tests/agents/test_plan_adversary_manifest.py` covers the approval-with-manifest path AND verifies the heading uses the `M1` ID so the post-approval parser self-check passes.
- 2.22.2 — `plan-review` SKILL.md updated to document the manifest-emission responsibility. file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.
- 2.22.3 — Plan-adversary tool surface adds scoped `Edit`/`Write` permission limited to the plan file path. file: `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`. test: writing to any other file path fails the agent's path-allowlist gate.
- 2.22.4 — Adversary self-check: after writing the manifest, calls `parse_plan(plan_path, parse_mode="expansion")` to validate the post-approval state strictly. On failure, the adversary retries the manifest write up to 3 times. After the cap is exhausted, behavior splits by yolo (R7.F-self-check-yolo): non-yolo escalates with `escalate_task(reason="needs_human:manifest_emission_failure:...")`; yolo NEVER calls `escalate_task` per the top-level invariant — instead writes a `## Yolo Fallbacks` audit section to the planning anchor description, falls back to the deterministic stub-manifest path (`emit_stub_manifest(plan_path)` from §1.7's `EmitStubManifest` action contract), and re-runs the strict parse. If even the stub fails (the deliverable schema in the plan itself is malformed beyond the emitter's reach), the agent appends a second audit marker and approves the plan with `mark_task_review_approved(approval_notes="yolo: manifest emission exhausted; stub fallback also failed; force-approved with no manifest — downstream gobby expand will reject this plan, requiring human intervention at expansion time")`. The pre-verdict review pass uses `parse_mode="draft"` so the loop does not deadlock on a not-yet-written manifest (§2.21.3). test: `tests/agents/test_plan_adversary_self_check.py` covers (a) non-yolo: draft-pass + expansion-fail + retry + escalate path; (b) yolo: same retry path then stub fallback succeeds; (c) yolo: stub fallback also fails → audit marker + force-approve; (d) explicit assertion that yolo NEVER calls `escalate_task` on this path.
- 2.22.5 — Adversary never edits the plan file when emitting findings (rejection rounds are review-only). test: `tests/agents/test_plan_adversary_no_edits_on_reject.py` confirms no plan-file diff is produced by a rejection round.

### 2.23 planner agent / plan-draft skill: fresh context + tighter mandate [category: config] (depends: 2.21, 2.22)

`kind: deliverable`

Target: `src/gobby/install/shared/skills/plan-draft/SKILL.md`; `src/gobby/install/shared/workflows/agents/planner.yaml` (if separate yaml exists; if not, document on the spawn-config path that wires plan-draft).

**Why**: Long-context drift across planner rounds caused the 26-round case where the agent re-interpreted findings each round and introduced new issues. Two fixes:

1. **Fresh context per round** — every planner revision spawns a new agent instance with no carryover from prior author rounds. Input: plan file + the round's adversary findings + a `## Plan Changelog` of prior round summaries. No accumulated session context drifting across rounds.
2. **Tighter mandate** — planner's job is "fill holes where the plan is incomplete or inconsistent with the codebase" and "escalate if premise is wrong." It is NOT to redesign or re-engineer in response to adversary findings. Re-engineering belongs in escalation, not in revision.

**Workflow per revision round**:

1. Read plan file + cumulative `## Plan Changelog` + latest `## Adversary Findings — Round N`.
2. Apply surgical fixes: missing acceptance items, ambiguous wording, contradictions with the codebase. Do NOT redesign.
3. Append summary to `## Plan Changelog`: "Round N+1 author: §X.Y — added missing test reference; §A.B — clarified depends_on annotation; ..." (one bullet per surgical fix).
4. Re-route the revised plan through `ExitPlanMode` for user re-approval (interactive mode) or trigger next adversary round automatically (delegated mode).

**Escalation path**: if a finding cannot be addressed by filling a hole — i.e., it requires redesigning a section or rejecting the premise — planner calls `escalate_task(reason="needs_human:premise_disagreement:<section_id>:<details>")`. User picks up the plan and decides: revise the requirements, accept the adversary finding, or override.

**Acceptance:**

- 2.23.1 — `plan-draft` SKILL.md updated with the "fresh context per round" requirement and the "fill holes, don't re-engineer; escalate if premise wrong" mandate. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`.
- 2.23.2 — `planner` agent (if separate yaml) is spawned with `clean_session=true` (or equivalent fresh-context flag) for every revision round; carries no context from prior rounds. file: `src/gobby/install/shared/workflows/agents/planner.yaml` (or the spawn-config path that wires plan-draft).
- 2.23.3 — `## Plan Changelog` section is required on every revised plan post-Round-1; planner appends a one-bullet summary per round. behavior: parser tolerates this section under existing `kind: framing`.
- 2.23.4 — Planner escalates on premise disagreement instead of re-engineering. test: `tests/agents/test_plan_author_escalation.py` covers a contrived "adversary suggests redesign" case; planner escalates rather than complies.
- 2.23.5 — Planner resubmit terminal contract (R7.F-planner-resubmit). After applying surgical fixes and appending the changelog bullet, the planner's terminal MCP transition is a single atomic call to `mark_task_needs_review(task_id=planning_anchor_id, review_notes="planner round N+1 complete; <bullet>")`. Per §1.8, `mark_task_needs_review` clears the `planning-current-verdict:rejected` label and sets the task back to `status=needs_review`. The atomicity is critical: the dispatcher's `rule_plan_rewrite_on_reject` (§1.7, ordered before `rule_plan_adversary`) gates on the rejected label, so any non-atomic clear-then-resubmit could leave a window where the rule re-fires and traps the task in the planner branch indefinitely. The planner agent's allowed_mcp_tools surface includes `mark_task_needs_review` and `escalate_task` (for premise-disagreement) but NOT `mark_task_review_approved`, `close_task`, or `mark_task_review_rejected`. file: `src/gobby/install/shared/workflows/agents/planner.yaml` (terminal step + tool surface). test: `tests/dispatch/test_planner_resubmit.py` covers the full rejection → planner rewrite → adversary re-review walk: (a) initial rejection sets `planning-current-verdict:rejected`, (b) rule_plan_rewrite_on_reject dispatches planner, (c) planner calls `mark_task_needs_review`, which clears the label atomically, (d) next tick fires `rule_plan_adversary` (NOT `rule_plan_rewrite_on_reject`) — the task is no longer trapped, (e) explicit assertion that no intermediate state has the label cleared without `status=needs_review`.

### 2.24 `/gobby plan` skill: end-to-end coordinator flow [category: config] (depends: 2.21, 2.22, 2.23)

`kind: deliverable`

Target: `src/gobby/install/shared/skills/plan/SKILL.md` — substantial rewrite.

**Why**: The current `/gobby plan` skill has the right shape (opt-in mode, adversarial loop, terminal cleanup) but is wired around the old design (combined planner/adversary, no manifest emission, no fresh-context-per-round, no delegated-flow handoff to build). This rewrite encodes the canonical end-to-end flow. **If anything below surprises the user when they read it, alignment is broken and the surprise is itself a finding.**

**Anchor-task contract (load-bearing)**:

The parent session (Claude in chat, this skill) **never claims a task**. Plan-markdown edits under `.gobby/plans/*.md` are exempt from `require-task-before-edit` (per `is_plan_file()` in `src/gobby/workflows/enforcement/blocking.py`), so no task is needed for plan authoring. The parent's role is pure orchestration — spawn agents, read verdict states, decide.

Each adversary round spawns against a **freshly-created anchor task** (child of the planning epic, `category: planning`). The anchor exists **only for verdict capture**: the adversary appends `## Adversary Findings — Round N` to the anchor's description and calls `mark_task_review_approved` (clean) or `mark_task_review_rejected` (with findings) on the anchor. Planner rounds (Round N > 1) get their own anchor task too — same pattern, different agent.

The parent **fires-and-forgets**: spawn the agent, end the turn cleanly. The parent has no claimed task, so the stop-hook (`require-task-close`) does not block end-of-turn. The daemon's task-completion notification (P2P signoff message from the agent's session) wakes the parent when the agent terminates. The parent's next turn reads the anchor's terminal state via `get_task` and routes to the next step:

- `status=review_approved` → advance to Phase 4 (expansion handoff) or Phase 3b (delegated build handoff).
- `status=open` after `mark_task_review_rejected` → if budget remains, spawn next round on a fresh anchor; if budget exhausted, surface final findings to user.
- `status=escalated` → surface escalation reason to user.

Anchor lifecycle: each anchor closes when its round terminates (verdict captured). Cleanup at loop end: any still-open anchors are closed; planning epic stays open until expansion handoff completes.

This pattern eliminates the parent-claim / parent-release dance and removes any stop-hook collisions. No polling, no `ScheduleWakeup`, no parent-side timers — the daemon notification is the only wake signal.

**Canonical flow**:

**Phase 1 — Initial plan authoring (Claude + user, interactive)**

1. User invokes `/gobby plan` in chat (or `/gobby plan <topic>`).
2. Skill loads `plan-draft` (and any related skills) via `gobby-skills:get_skill`.
3. Claude collaborates with the user to draft the plan: requirements gathering, structure, contract-formatting per `plan-draft`. No spawned agents yet — Claude IS the planner for the first draft.
4. Claude presents the draft via native `ExitPlanMode`.
5. User approves the first draft. Rejections at this stage loop back to step 3 with the user driving revisions; no adversary involvement yet.

**Phase 2 — Mode selection (post-first-draft-approval)**

1. Skill prompts the user via `AskUserQuestion`: **Interactive** (per-round adversary findings shown to user; user re-approves each revised plan) or **Delegated** (silent revision loop until terminal; user only sees terminal outcome). Also prompts for `max_rounds` (default = `BuildConfig.max_review_rounds`, configurable per §2.19).

**Phase 3a — Adversarial review loop (fire-and-forget orchestration)**

After Phase 1 the parent's role pivots from "plan writer" to "plan coordinator" — same session, different mode. The coordinator never stays alive across rounds. Each round is a single spawn-then-end-turn cycle, with the daemon's task-completion notification as the wake signal for the next turn. The fire-and-forget pattern applies in BOTH interactive and delegated modes; the only difference is whether the coordinator inserts a user-confirmation gate between rounds (interactive) or routes straight to the next spawn (delegated). Round numbering: user-facing 1-indexed; internally 0-indexed per existing convention.

1. **Round 1 spawn**: parent creates a fresh anchor task `Plan-adversary review — round 1` (child of the planning epic, `category: planning`). Parent spawns `plan-adversary` (LLM B, fresh context, no isolation) against the anchor with the current plan file. Parent ends the turn. No claim, no `ScheduleWakeup`.
2. **Round N spawn (N > 1)**: parent creates a fresh anchor `Planner revision — round N` and spawns `planner` (LLM A, fresh context per §2.23) with the plan file + cumulative `## Plan Changelog` + `## Adversary Findings — Round N-1` from the prior anchor. Parent ends the turn. On planner wake (anchor terminal), parent creates `Plan-adversary review — round N`, spawns adversary, ends the turn.
3. **Wake-and-route**: when the daemon wakes the parent (task-completion notification on the active anchor), parent reads anchor terminal state via `get_task` and branches by mode:
   - `status=review_approved` (adversary clean) → adversary already appended the `## Task Manifest` YAML to the plan file (per §2.22). Parent advances to **Phase 4** (or Phase 3b for delegated mode). Same in both modes.
   - `status=open` after `mark_task_review_rejected` (adversary findings on anchor description):
     - **Interactive**: parent presents findings to user via `ExitPlanMode` or `AskUserQuestion`, asking whether to continue with the next revision round. User approves → parent ends turn after spawning next round; user aborts → parent runs cleanup. The user-confirmation gate happens in the parent's chat session; the parent does NOT stay alive waiting for it — the parent ends the turn after presenting and the user's reply triggers the next turn.
     - **Delegated**: if budget remains, parent silently spawns the next round on a fresh anchor, ends turn. No user prompt.
     - Both modes: if budget exhausted, parent surfaces findings + terminal options.
   - `status=escalated` → parent surfaces escalation reason verbatim. Same in both modes.
4. **Round-budget exhaustion**: parent presents the final adversary findings + offers terminal options (revise manually + run `gobby build` directly, abort + close planning epic, restart with fresh budget). Each terminal option runs cleanup (close all open anchors).
5. **Anchor cleanup**: each anchor closes when its round terminates (its verdict has been captured). At loop end (approval, exhaustion, abort, or restart), any still-open anchors are closed. The planning epic stays open until Phase 4 expansion handoff completes.
6. **Parent role**: never reviews or revises the plan. Never claims a task. Never schedules wakeups. Pure orchestration — spawn agent → end turn → daemon-wake → read anchor state → route. **No `ScheduleWakeup`, no `Monitor`, no polling. The daemon's task-completion notification is the only wake signal.**

**Phase 3b — Delegated build handoff (alternative flow only when user picked "Delegated" in Phase 2)**

This phase fires after the interactive review loop terminates (approval or exhaustion). It does NOT replace Phase 3a — Phase 3b is a follow-on once the plan is approved-or-final.

1. Skill loads the `build` skill via `gobby-skills:get_skill`.
2. Skill asks the user via `AskUserQuestion`: what scope of build do you want?
    - **Plan only** — adversary already approved; no expansion fires. User runs expansion later.
    - **Plan + test_arch** — run plan-adversary (already done) → test architect (per existing `rule_test_arch`).
    - **Plan + test_arch + expand** — through expansion only; user runs dev/qa/holistic later.
    - **Plan + full build** (test_arch → expand → dev → qa → holistic → pr → merge) — the killer-feature path.
3. Based on the choice, the skill EITHER:
    - Triggers `gobby build <plan_file> --profile <resolved>` in this session (immediate execution), OR
    - Provides the CLI command to the user as a string for them to run at their convenience.
4. Skill exits.

**Phase 4 — Expansion handoff (post-adversary-approval, regardless of interactive vs. delegated)**

1. Skill calls `start_expansion_run(task_id=plan_parent_ref, plan_file=artifact_path, auto_apply=true)`. Fire-and-forget — no parent claim. End the turn.
2. Daemon wakes parent on expansion-run completion.
3. On success: report child-task count to user; close planning epic; close any remaining anchor tasks.
4. On failure: surface error; offer retry / retry-with-overrides / escalate per existing skill design.

**Acceptance:**

- 2.24.1 — `plan/SKILL.md` rewritten to encode the canonical flow above. Phases, step numbering, agent spawn shapes, and terminal cleanup paths match this document. file: `src/gobby/install/shared/skills/plan/SKILL.md`. test: `tests/skills/test_plan_skill_flow.py` exercises each phase via mocked spawn / completion notifications.
- 2.24.2 — Round-1 adversary spawn is against the user-approved first draft, no planner involvement. test: `tests/skills/test_plan_skill_flow.py::test_round_1_no_plan_author`.
- 2.24.3 — Round-N (N > 1) spawns planner with fresh context first, then plan-adversary against the revised plan. Planner input includes cumulative `## Plan Changelog` + latest `## Adversary Findings`. test: `tests/skills/test_plan_skill_flow.py::test_round_n_spawns_author_then_adversary`.
- 2.24.4 — On adversary approval, manifest emission is the trigger for expansion handoff. behavior: skill calls `start_expansion_run` only after manifest YAML is present in plan file. test: `tests/skills/test_plan_skill_flow.py::test_expansion_after_manifest`.
- 2.24.5 — Delegated build handoff (Phase 3b) loads `build` skill, prompts for scope, dispatches or hands back CLI. file: `src/gobby/install/shared/skills/plan/SKILL.md`. test: `tests/skills/test_plan_skill_flow.py::test_delegated_build_handoff`.
- 2.24.6 — Coordinator role enforced: skill does NOT edit the plan file during Phase 3. test: `tests/skills/test_plan_skill_flow.py::test_no_edits_during_review_loop` asserts no Edit/Write tool calls fire from this skill's session during Phase 3 rounds.
- 2.24.7 — Anchor-task contract: parent never claims a task; each round spawns against a freshly-created anchor (`category: planning`, child of the planning epic) that exists solely for verdict capture. file: `src/gobby/install/shared/skills/plan/SKILL.md`. test: `tests/skills/test_plan_skill_flow.py::test_anchor_task_per_round` asserts (a) parent has no claimed task before / during / after spawn, (b) each adversary spawn gets its own anchor, (c) anchor closes when its round terminates.
- 2.24.8 — Fire-and-forget orchestration: parent ends the turn after each spawn; daemon's task-completion notification is the only wake signal. No `ScheduleWakeup`, no `Monitor`, no polling. test: `tests/skills/test_plan_skill_flow.py::test_no_polling_or_scheduled_wakeup` asserts the skill never invokes ScheduleWakeup or Monitor during Phase 3a.
- 2.24.9 — Wake-and-route: on daemon wake, parent reads the active anchor's terminal state via `get_task` and routes per the anchor-task contract (review_approved → Phase 4 / Phase 3b; rejected + budget remaining → next round spawn; rejected + budget exhausted → surface findings; escalated → surface reason). test: `tests/skills/test_plan_skill_flow.py::test_wake_routing_by_anchor_status`.
- 2.24.10 — Mode-agnostic coordinator: fire-and-forget orchestration applies in both interactive and delegated modes. The only mode-specific behavior is the user-confirmation gate after a rejected round (interactive: present findings + ask whether to continue; delegated: silently spawn next round). Parent never stays alive across rounds in either mode. test: `tests/skills/test_plan_skill_flow.py::test_interactive_and_delegated_share_orchestration` covers both modes against the same anchor / spawn / wake-route mechanics.

### 2.20 Re-expansion of #12725 as Epic 1 end-to-end validation [category: manual] (depends: 2.21, 2.22)

`kind: deliverable`

Target: live execution against the running daemon — re-expand `#12725` against the revised `task-12725-lifecycle-dispatch-rev1.md` once plan-adversary approves it; verify the deterministic compile path produces a covered manifest.

**Why this is a deliverable, not a verification afterthought**: Epic 1 (#13175, the Plan-Coverage Contract) was built so that contract-conforming plans could be expanded mechanically into task trees with auto-populated `covers:` labels. The contract has never been exercised end-to-end on a real-world plan with the new design. This Epic-2 plan is the test case: it includes everything from typed sections to acceptance items to phase boundaries to dependency annotations, and re-expanding it under the deterministic compile path is the validation that Epic 1's foundation actually works. Without this validation, we don't know whether Epic 1 shipped a working contract or just a structurally-compliant one.

**Pre-conditions**:

- §2.21 (Task Manifest contract) and §2.22 (plan-adversary manifest emission on approval) are **implemented**. These are the only sections whose implementation §2.20 depends on, because the deterministic compile path consumes the manifest written by the approving adversary. The narrow `(depends: 2.21, 2.22)` annotation reflects this.
- All other §2.X deliverables (§2.11–§2.19, §2.23–§2.24) need only be **specified in the plan** with acceptance items the plan-adversary has approved. Their implementation is downstream of §2.20 — re-expansion creates the leaves under which that implementation work happens. Their `kind: deliverable` shape and acceptance items get parser-validated when the deterministic compile path reads the plan; they do not need to be coded before §2.20 fires.
- `task-12725-lifecycle-dispatch-rev1.md` has its `plan_hash` up to date and a fresh coverage manifest at `.gobby/plans/coverage/<project_id>/12725/task-12725-lifecycle-dispatch-rev1.coverage.yaml`.
- The deterministic compile path from commit `54ad154fb` is live in the running daemon.

This sequencing makes §2.20 a pre-implementation gate, not a late smoke test: it proves Epic 1's contract can compile this plan into a coverable task tree before the bulk of §2.X implementation work begins. If §2.20 fails, that signal is most valuable BEFORE the implementation tasks start; landing it after them is exactly the regression mode this revision aims to avoid.

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

### 3.2 Build service — CLI + MCP + HTTP shared core [category: code]

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
- 3.2.5 — `_kick_dispatcher_tick()` triggers an immediate dispatcher heartbeat after build-state writes (replaces the current placeholder that returns `0`); the periodic cron continues to fire independently. R7.F-dispatcher-services / R7.F-live-config / R7.F-build-config-on-services: this function calls `get_app_context().dispatcher_services` to read the live `DispatcherServices` instance (set on `ServiceContainer` in `runner_init.py` per §1.10.2) and reads `max_active_agents` from `services.build_config.max_active_agents` — `build_config` is a first-class field on `DispatcherServices` per the dataclass definition in §1.9. It then calls `run_tick(services, holder, max_active)` — the same path the cron handler uses, so expansion-impl dependencies (task_manager, llm_service, config, build_config, completion_registry, triggering_session_id) are wired identically in both surfaces. symbol: `gobby.build.service._kick_dispatcher_tick`. behavior: returns the count of tasks dispatched by the kicked tick (the same shape `BuildResult.tick_dispatched` exposes). test: `tests/build/test_kick_dispatcher_tick_services.py` covers (a) the function reads `DispatcherServices` via `get_app_context().dispatcher_services`, (b) it constructs no fresh services (no double-wiring), (c) cron and immediate-kick paths produce identical TickReports for an equivalent tick state, (d) `max_active_agents` is read from `services.build_config`, NEVER from `get_app_context().config` (which is `DaemonConfig`).
- 3.2.6 — `BuildOptions.target_branch=None` resolves to `git rev-parse --abbrev-ref HEAD` at service-call time before any `task_artifacts` write; explicit `--target-branch <name>` is preserved as-is. behavior: `task_artifacts.target_branch` is never persisted as `None` for plan-file or epic builds. Leaf builds (which force `isolation=none` per the leaf-input contract) leave `target_branch` UNSET — no `set_artifact("target_branch", ...)` call is made on the leaf path. file: `src/gobby/build/service.py`. test: `tests/build/test_target_branch.py` covers the default-resolve path, the explicit-override path, and the leaf-unset path.
- 3.2.7 — `--skip-stage` validation: the build service rejects any value not in `SKIPPABLE_STAGES = {"plan_review", "test_arch", "expanding", "qa", "holistic_review", "pr"}`. Specifically `--skip-stage merging`, `--skip-stage dev`, `--skip-stage worktree` raise a clear `BuildError` naming the rejected stage and listing the skippable set. Validation fires before any `task_artifacts` or label write so partial state is impossible. file: `src/gobby/build/service.py`. test: `tests/build/test_skip_stage_validation.py` covers (a) each non-skippable stage name rejected, (b) skippable stages accepted, (c) comma-separated list rejected if any element is non-skippable, (d) error message names the failing stage and lists the skippable set.
- 3.2.8 — Manifest precondition for `--skip-stage plan_review` (R7.F-bypass-manifest): plan-file and epic builds with `--skip-stage plan_review` MUST have an existing `## M1 Task Manifest` that parses cleanly under `parse_plan(plan_path, parse_mode="expansion")`. The build service runs the parser at validation time and rejects with `BuildError("--skip-stage plan_review requires a pre-emitted ## M1 Task Manifest in <plan_path>; either run plan-adversary first or remove --skip-stage plan_review")` when the parse fails. Leaf builds are unaffected (no plan file). file: `src/gobby/build/service.py`. test: `tests/build/test_skip_plan_review_manifest_gate.py` covers (a) plan with manifest passes, (b) plan without manifest rejected with the error message above, (c) plan with malformed manifest rejected, (d) leaf builds with `--skip-stage plan_review` accepted (no plan file to validate).

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
  - `rule_plan_adversary` rounds-exhausted path: non-yolo escalates; yolo emits the exact action sequence `[AppendAuditMarker, EmitStubManifest, AdvanceLifecycle(test_arch)]` (R7.F-yolo-manifest-fallback). Test asserts the action types, order, and the `EmitStubManifest.task_id` matches the planning anchor.
  - `rule_merging` passes `yolo` as an initial variable to the merge agent.
  - `rule_create_worktree` (R4.F6) reads `task_artifacts.target_branch`; falls back to `git rev-parse --abbrev-ref HEAD` when artifact absent; passes resolved branch to `CreateWorktree.base_branch`. Fires only for `isolation=worktree`.
  - `rule_create_clone` (R4.F2) fires only for `isolation=clone`; emits `CreateClone(epic_task_id, base_branch=<target>)`; no-ops when `clone_path` already set; respects `_has_isolation_artifact` mutual exclusion with worktree.
- `tests/dispatch/test_rule_expansion.py` (R4.F1 + R7.F-failed-run) — covers the `rule_start_expansion` / `rule_validate_expansion` split:
  - `rule_start_expansion` emits `StartExpansionRun` when `expansion_run_id` is NULL and attempts < cap.
  - `rule_start_expansion` no-ops while `expansion_run_id` is set and run is not terminal.
  - `rule_validate_expansion` spawns `expansion-qa` once `expansion_run_id` is set and the run state is **`completed`**; no-op when `claimed_by_session_id` is set (candidate scan filters during active QA).
  - **`rule_validate_expansion` ALSO spawns `expansion-qa` when the run state is `failed`** (R7.F-failed-run) — the `_expansion_run_terminal` predicate covers both terminal states, so failed runs are not stranded. test: `test_rule_validate_expansion_dispatches_qa_on_failed_run` constructs a task with `expansion_run_id` pointing at a `failed` run and asserts the rule emits `SpawnAgent(agent="expansion-qa")` exactly as for the completed case.
  - On expansion-qa rejection of a failed run (mocked via `mark_task_review_rejected(lifecycle=expanding)`), `expansion_run_id` clears and `expansion_attempts` increments — same path as for a structurally-invalid completed run. test: `test_failed_run_rejection_clears_artifact_and_increments_attempts` covers the rejection path side-effects on the artifact row.
  - On expansion-qa rejection of a completed-but-invalid run (mocked via `mark_task_review_rejected`), `expansion_run_id` clears and `expansion_attempts` increments; next tick `rule_start_expansion` re-fires.
  - At `expansion_attempts >= MAX_EXPANSION_ATTEMPTS` (resolved per §1.7 / §2.19), non-yolo emits `EscalateTask`; yolo emits `AppendAuditMarker + AdvanceLifecycle(in_development)`.
- `tests/tasks/test_expansion_agent_selection.py` — seed a plan with a mix of FE/BE/ambiguous `### N.N` sections; run expansion with mocked LLM output against a mocked `list_agent_definitions`; assert every code leaf has `assigned_agent` populated; assert ambiguous leaves default to `backend-developer` and produce an `## Agent Selection` description marker.
- `tests/tasks/test_expansion_qa_transitions.py` (R3.F3) — expansion-qa calls `mark_task_review_approved` on success and `mark_task_review_rejected` on missing `assigned_agent`; fabricate both paths and assert the parent epic's lifecycle/status moves correctly.
- `tests/dispatch/test_dispatcher.py` — end-to-end tick with mocked `execute_spawn`; assert TOCTOU re-evaluation under lock; assert agent-slot cap honored; assert `TickReport` is persisted to `~/.gobby/logs/dispatcher.jsonl` as structured JSON.
- `tests/dispatch/test_cron_registration.py` — handler registered under name "state-dispatcher"; cron row inserted idempotently.
- `tests/storage/test_transitions_lifecycle.py` — extended review tools advance lifecycle at the right boundaries; planner-resubmit clears rejection marker; **R3.F4**: holistic approval advances `holistic_review → pr`; status reset on advance (review_approved → open); `advance_lifecycle(reason, by_actor)` writes a `task_lifecycle_events` row; `de_escalate_task(lifecycle=...)` is a single-call recovery that updates both status and lifecycle in one transaction.
- `tests/storage/test_holistic_rejection.py` (R4.F5) — `mark_task_review_rejected(epic, cited_subtasks=[...])` on `lifecycle=holistic_review` atomically appends findings, reopens cited subtasks (`(holistic_review, review_approved | closed) → (in_development, open)`), and rewinds the epic lifecycle (`holistic_review → in_development`) in a single transaction; assert atomicity by injecting a mid-transaction failure and checking nothing partially applied. Rejecting `lifecycle=holistic_review` without `cited_subtasks` (or with `[]`) raises a validation error. Confirm `rule_all_leaves_holistic` (renamed from `rule_all_closed_advance_to_holistic`, §1.7) does NOT immediately re-fire because at least one cited leaf is now `open` at `lifecycle=in_development`. Cover the escalate-rescope third path: `escalate_task(epic, reason="needs_human:rescope_required:...")` flips the epic to `status=escalated`, leaves `lifecycle=holistic_review` unchanged.
- `tests/storage/test_expansion_rejection.py` (R4.F1) — `mark_task_review_rejected` on `lifecycle=expanding` clears `task_artifacts.expansion_run_id` and increments `expansion_attempts` in a single transaction; `mark_task_review_approved` on `lifecycle=expanding` does NOT clear those fields (audit trail of which run produced the approved tree).
- `tests/build/test_target_branch.py` (R4.F6) — `gobby build` captures `git rev-parse --abbrev-ref HEAD` by default; `--target-branch <name>` overrides; missing branch raises with the available-branches list; `task_artifacts.target_branch` is populated on plan-file and epic builds; **leaf builds (which force `isolation=none` per §3.2) leave `target_branch` UNSET** because in-branch work has no separate target — the test asserts the leaf-build code path does not call `set_artifact("target_branch", ...)`; `rule_create_worktree` consumes it on the epic path; legacy epic without artifact row falls back to current HEAD.
- `tests/dispatch/test_clone_dispatch.py` (R4.F2 in-scope, R6.F1 grounded) — `_dispatch` for `CreateClone` invokes `CloneIsolationHandler.prepare_environment(SpawnConfig(...))` (the high-level API that composes `LocalCloneManager.create` + `CloneGitManager.create_clone` + bootstrap); persists both `clone_path` and `clone_id` into `task_artifacts` via a single `set_artifacts_atomic` call; the SQL CHECK constraint blocks the write if a worktree artifact already exists. `_resolve_cwd(leaf, agent_name="developer")` routes into the clone path; `_resolve_cwd(epic, agent_name="merge")` returns repo root.
- `tests/dispatch/test_merge_integration.py` (R4.F7 + R6.F3) — exercises §2.10 with the existing tool-driven contract:
  - Clean worktree merge: `merge_worktree(worktree_id, push=true)` returns success → agent runs cleanup FIRST (R7.F-cleanup-ordering): `mark_worktree_merged(worktree_id)`, `delete_worktree(worktree_id)`, `clear_isolation_pair(epic_task_id, "worktree")` — THEN `mark_task_review_approved(approval_notes=...)` as the LAST step; lifecycle advances to `merged`, status `closed`. Cleanup-failure handling SPLITS by yolo (R7.F-yolo-cleanup): non-yolo calls `escalate_task("needs_human:merge_cleanup_failed:<step>:<details>")` and the epic stays at `lifecycle=merging, status=escalated`; yolo NEVER calls `escalate_task` — instead emits `[append_description_section(heading="Yolo Fallbacks", body=...), advance_lifecycle(to=Lifecycle.merged, reason="yolo: merge cleanup failed; force-advanced with artifacts preserved", by_actor="merge")]`, leaving artifacts uncleared for human inspection. Merge SHA capture deferred to #12728 (R7.F3) — `merge_commit_sha` stays NULL.
  - Clean clone merge: `sync_clone(clone_id)` then `merge_clone(clone_id, target_branch)` returns success → cleanup FIRST (`delete_clone` and `clear_isolation_pair(epic_task_id, "clone")` iff `cleanup_clones_on_merge=true`), then `mark_task_review_approved` LAST. Same yolo-split cleanup-failure handling: non-yolo escalates, yolo force-advances via `advance_lifecycle` per the top-level "yolo never escalates" invariant.
  - Worktree conflict resolved by AI: `merge_worktree` returns has_conflicts=true → AI flow (`merge_start` → `merge_resolve` → `merge_apply`) resolves → `merge_worktree(push=true)` succeeds → success path.
  - Worktree conflict, AI fails (non-yolo): `merge_abort` then `escalate_task` with `de_escalate_task` instructions in reason; lifecycle stays `merging`, status `escalated`.
  - Yolo conflict, single attempt fails: `merge-attempts:1` label applied; `mark_task_review_rejected` called on `lifecycle=merging` (R6.F4 case); status resets to `open`; lifecycle stays `merging`. Next tick re-dispatches.
  - Yolo conflict, retries exhausted (`merge-attempts:N >= cap`): `gobby-tasks-ops:append_description_section(heading="Yolo Fallbacks", body=...)` + `gobby-tasks:advance_lifecycle(task_id, to=Lifecycle.merged, reason="yolo: merge attempts exhausted; force-advanced without merge", by_actor="merge")`; lifecycle advances to `merged, status=closed` via the explicit transition (not `mark_task_review_approved`, which would deadlock against the stage-advance gate); isolation artifact pair NOT cleared (preserved for inspection). No `escalate_task` ever called.
- `tests/storage/test_artifact_xor.py` (R6.F2 + R7.F4) — SQL CHECK enforces all three predicates: pairwise co-presence within each isolation family, family XOR, plus rejecting partial states (`worktree_path` set with `worktree_id` NULL or vice versa). `set_artifacts_atomic` raises with a clear error mapping the failing predicate; `clear_isolation_pair("worktree")` clears the worktree pair atomically and lets a subsequent clone-pair write succeed.
- `tests/mcp_proxy/test_tasks_ops_artifacts.py` (R7.F3 / §1.1d) — covers the new MCP tools: `set_artifact` with each valid field; `set_artifact` with an invalid field name surfaces an allowlist error; `set_artifacts_atomic` writes a `(worktree_path, worktree_id)` pair atomically; `clear_isolation_pair("clone")` clears `clone_path` + `clone_id` together; `append_description_section` appends a `## {heading}\n{body}\n` block and is idempotent on duplicate `(heading, body)` calls within the same transaction; `get_artifacts` returns the row dict or empty when absent.
- `tests/build/test_isolation_validation.py` (R6.F2) — `build <#leafref> --isolation worktree` raises with leaf-isolation-rejected message; `build <#taskref> --isolation clone` on an epic that already has `worktree_path` raises with the change-isolation-rejected message; `build <#taskref>` on a not-yet-built epic accepts isolation cleanly.
- `tests/storage/test_merging_rejection.py` (R6.F4) — `mark_task_review_rejected` on `lifecycle=merging` leaves lifecycle unchanged, resets status to `open`, appends findings, writes a `task_lifecycle_events` row. `cited_subtasks` ignored on this lifecycle.
- `tests/tasks/test_start_expansion_run_impl.py` (R6.F5, aligned with §2.8b) — `start_expansion_run_impl` is exported from `src/gobby/mcp_proxy/tools/tasks/_expansion.py` so the dispatcher can import it directly without going through MCP transport. The impl creates an expansion-run row via `LocalExpansionRunManager.create`, kicks `compile_run`, and returns the persisted `ExpansionRun` (or `run.id`) so the dispatcher's `StartExpansionRun` action handler can persist it via `set_artifact("expansion_run_id", run.id)`. Failure during `compile_run` flips `run.status` to `failed` but the row is still returned (expansion-qa picks up failed runs and rejects via §2.9). **No `ExpansionService.start_run(...)` wrapper exists** — §2.8b explicitly drops that wrapper in favor of exporting the existing impl. Test asserts (a) the export is importable, (b) the dispatcher path calls it directly and persists the returned id, (c) compile failures are recoverable through expansion-qa rejection.
- `tests/agents/test_merge_integration.py` (R4.F7) — exercises the §2.10 finalize step:
  - Clean merge → cleanup FIRST (R7.F-cleanup-ordering): `mark_worktree_merged` + `delete_worktree` + `clear_isolation_pair` for worktree (or `delete_clone` + `clear_isolation_pair` for clone). THEN `mark_task_review_approved(epic, approval_notes=...)` as the LAST step; lifecycle advances to `merged`, status `closed`. Cleanup-failure handling SPLITS by yolo (R7.F-yolo-cleanup): non-yolo emits `escalate_task("needs_human:merge_cleanup_failed:...")` (lifecycle stays `merging, status=escalated`); yolo emits `[append_description_section("Yolo Fallbacks", ...), advance_lifecycle(to=Lifecycle.merged, ..., by_actor="merge")]` and NEVER calls `escalate_task`. `merge_commit_sha` stays NULL (deferred to #12728 per R7.F3).
  - Non-yolo conflict → `escalate_task` called with reason starting `needs_human:`; reason text includes `de_escalate_task(... target_status='open', lifecycle=Lifecycle.merging ...)` instructions so recovery routes back through `rule_merging` and the merge agent's success cleanup runs (worktree marked merged, artifact pair cleared, lifecycle terminal write-back). Test asserts the escalate reason names `lifecycle=Lifecycle.merging` (NOT `merged`) and that a follow-up de-escalation + tick produces a clean cleanup trace.
  - Yolo conflict, single-attempt failure → `mark_task_review_rejected` called; `merge-attempts:1` label applied; lifecycle stays `merging, status=open`.
  - Yolo conflict, retries exhausted (`merge-attempts:N >= cap`) → `append_description_section(heading="Yolo Fallbacks", ...)` + `advance_lifecycle(epic, to=Lifecycle.merged, reason="yolo: merge attempts exhausted; force-advanced without merge", by_actor="merge")` (advance_lifecycle, not mark_task_review_approved — the stage-advance gate at lifecycle=merging would block the fallback); lifecycle advances to `merged, status=closed`; isolation pair NOT cleared (preserved); no escalate ever called.
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
2. `uv run gobby build .gobby/plans/task-12725-lifecycle-dispatch-rev1.md --profile full --max-review-rounds 2 --target-branch main`
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

## M1 Task Manifest

`kind: manifest`

```yaml
- title: Extend task CRUD for new fields and helpers
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/storage/tasks/_crud.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.3:1.3.1
  - covers:task-12725-lifecycle-dispatch-rev1:1.3:1.3.2
  - covers:task-12725-lifecycle-dispatch-rev1:1.3:1.3.3
  - covers:task-12725-lifecycle-dispatch-rev1:1.3:1.3.4
  - covers:task-12725-lifecycle-dispatch-rev1:1.3:1.3.5
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.3'
- title: '`is_blocked_by_deps` predicate'
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: src/gobby/storage/tasks/_dependencies.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.3a:1.3a.1
  assigned_agent: backend-developer
  tdd: true
  source_section: 1.3a
- title: Dispatcher mutex
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: src/gobby/dispatch/mutex.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.4:1.4.1
  - covers:task-12725-lifecycle-dispatch-rev1:1.4:1.4.2
  - covers:task-12725-lifecycle-dispatch-rev1:1.4:1.4.3
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.4'
- title: Register mutex-clearing event handlers
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: src/gobby/hooks/event_handlers/_task.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.5:1.5.1
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.5'
- title: Dispatch action wrappers
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: src/gobby/dispatch/actions.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.6:1.6.1
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.6'
- title: Prompt-builder registry
  category: code
  task_type: feature
  depends_on:
  - '1.6'
  validation_criteria: src/gobby/dispatch/prompts.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.6a:1.6a.1
  - covers:task-12725-lifecycle-dispatch-rev1:1.6a:1.6a.2
  - covers:task-12725-lifecycle-dispatch-rev1:1.6a:1.6a.3
  assigned_agent: backend-developer
  tdd: true
  source_section: 1.6a
- title: Decision rules for all stages
  category: code
  task_type: feature
  depends_on:
  - '1.6'
  - 1.6a
  validation_criteria: src/gobby/dispatch/rules.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.7:1.7.1
  - covers:task-12725-lifecycle-dispatch-rev1:1.7:1.7.2
  - covers:task-12725-lifecycle-dispatch-rev1:1.7:1.7.3
  - covers:task-12725-lifecycle-dispatch-rev1:1.7:1.7.4
  - covers:task-12725-lifecycle-dispatch-rev1:1.7:1.7.5
  - covers:task-12725-lifecycle-dispatch-rev1:1.7:1.7.6
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.7'
- title: Lifecycle transitions in review tools
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/storage/tasks/_transitions.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.1
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.2
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.3
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.4
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.5
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.6
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.6a
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.6b
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.6c
  - covers:task-12725-lifecycle-dispatch-rev1:1.8:1.8.7
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.8'
- title: Dispatcher scanner
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '1.6'
  - '1.7'
  - '1.8'
  - 2.8b
  validation_criteria: src/gobby/dispatch/dispatcher.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.9:1.9.1
  - covers:task-12725-lifecycle-dispatch-rev1:1.9:1.9.2
  - covers:task-12725-lifecycle-dispatch-rev1:1.9:1.9.5
  - covers:task-12725-lifecycle-dispatch-rev1:1.9:1.9.3
  - covers:task-12725-lifecycle-dispatch-rev1:1.9:1.9.4
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.9'
- title: Cron handler registration
  category: code
  task_type: feature
  depends_on:
  - '1.9'
  validation_criteria: src/gobby/dispatch/cron_registration.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:1.10:1.10.1
  - covers:task-12725-lifecycle-dispatch-rev1:1.10:1.10.5
  - covers:task-12725-lifecycle-dispatch-rev1:1.10:1.10.2
  - covers:task-12725-lifecycle-dispatch-rev1:1.10:1.10.4
  - covers:task-12725-lifecycle-dispatch-rev1:1.10:1.10.3
  assigned_agent: backend-developer
  tdd: true
  source_section: '1.10'
- title: 'Expansion: Agent Selection + profile-appropriate subtasks'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/tasks/expansion_service.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.8:2.8.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.8:2.8.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.8:2.8.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.8:2.8.4
  assigned_agent: backend-developer
  tdd: true
  source_section: '2.8'
- title: Expose `start_expansion_run_impl` for in-process dispatcher use
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/mcp_proxy/tools/tasks/_expansion.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.8b:2.8b.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.8b:2.8b.2
  assigned_agent: backend-developer
  tdd: true
  source_section: 2.8b
- title: Split `expansion_service.py` monolith
  category: refactor
  task_type: feature
  depends_on:
  - '2.8'
  - 2.8b
  validation_criteria: src/gobby/tasks/expansion/service.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.8c:2.8c.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.8c:2.8c.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.8c:2.8c.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.8c:2.8c.4
  assigned_agent: backend-developer
  tdd: true
  source_section: 2.8c
- title: Expansion-QA transition contract
  category: config
  task_type: feature
  depends_on:
  - '1.8'
  validation_criteria: src/gobby/install/shared/workflows/agents/expansion-qa.yaml
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.9:2.9.1
  assigned_agent: developer
  tdd: false
  source_section: '2.9'
- title: "Merge agent \u2014 lifecycle integration"
  category: config
  task_type: feature
  depends_on:
  - '1.8'
  validation_criteria: src/gobby/install/shared/workflows/agents/merge.yaml
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.10:2.10.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.10:2.10.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.10:2.10.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.10:2.10.4
  assigned_agent: developer
  tdd: false
  source_section: '2.10'
- title: qa-reviewer agent (read-only)
  category: config
  task_type: feature
  depends_on:
  - '1.7'
  - '1.8'
  validation_criteria: src/gobby/install/shared/workflows/agents/qa-reviewer.yaml
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.11:2.11.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.11:2.11.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.11:2.11.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.11:2.11.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.11:2.11.5
  assigned_agent: developer
  tdd: false
  source_section: '2.11'
- title: holistic-reviewer three-outcome contract
  category: config
  task_type: feature
  depends_on:
  - '1.7'
  - '1.8'
  - '2.11'
  validation_criteria: src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.12:2.12.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.12:2.12.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.12:2.12.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.12:2.12.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.12:2.12.5
  assigned_agent: developer
  tdd: false
  source_section: '2.12'
- title: expansion-qa as a multi-mode verification harness
  category: config
  task_type: feature
  depends_on:
  - '2.9'
  validation_criteria: src/gobby/install/shared/workflows/agents/expansion-qa.yaml
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.13:2.13.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.13:2.13.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.13:2.13.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.13:2.13.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.13:2.13.5
  assigned_agent: developer
  tdd: false
  source_section: '2.13'
- title: Hook/rule scoping for autonomous build agents
  category: code
  task_type: feature
  depends_on:
  - '2.11'
  - '2.12'
  validation_criteria: src/gobby/agents/roles.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.14a:2.14a.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.14a:2.14a.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.14a:2.14a.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.14a:2.14a.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.14a:2.14a.5
  assigned_agent: backend-developer
  tdd: true
  source_section: 2.14a
- title: Deprecation pattern for retired agents and pipelines
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/install/shared/workflows/agents/deprecated/.gitkeep
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.14b:2.14b.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.14b:2.14b.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.14b:2.14b.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.14b:2.14b.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.14b:2.14b.5
  - covers:task-12725-lifecycle-dispatch-rev1:2.14b:2.14b.6
  - covers:task-12725-lifecycle-dispatch-rev1:2.14b:2.14b.7
  assigned_agent: developer
  tdd: false
  source_section: 2.14b
- title: DB-backed plan state + `gobby-plans` MCP/CLI
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/storage/migrations/<next_version>_add_plans_table.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.15:2.15.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.15:2.15.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.15:2.15.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.15:2.15.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.15:2.15.5
  - covers:task-12725-lifecycle-dispatch-rev1:2.15:2.15.6
  - covers:task-12725-lifecycle-dispatch-rev1:2.15:2.15.7
  assigned_agent: backend-developer
  tdd: true
  source_section: '2.15'
- title: Retire `.grandfathered`, `.grandfathered-task-state.yaml`, `.legacy-classification.yaml`
  category: code
  task_type: feature
  depends_on:
  - '2.15'
  validation_criteria: files no longer exist in the repo (`git ls-files` returns no
    matches)
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.16:2.16.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.16:2.16.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.16:2.16.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.16:2.16.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.16:2.16.5
  - covers:task-12725-lifecycle-dispatch-rev1:2.16:2.16.6
  assigned_agent: backend-developer
  tdd: true
  source_section: '2.16'
- title: System-managed coverage manifest lifecycle
  category: code
  task_type: feature
  depends_on:
  - '2.15'
  validation_criteria: "idempotent \u2014 existing manifest is overwritten"
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.17:2.17.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.17:2.17.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.17:2.17.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.17:2.17.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.17:2.17.5
  assigned_agent: backend-developer
  tdd: true
  source_section: '2.17'
- title: Auto-move plan files on epic terminal state
  category: code
  task_type: feature
  depends_on:
  - '1.7'
  - '2.15'
  validation_criteria: "`tests/dispatch/test_archive_plan_on_merge.py` (cross-references\
    \ \xA71.7.5)"
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.18:2.18.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.18:2.18.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.18:2.18.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.18:2.18.4
  assigned_agent: backend-developer
  tdd: true
  source_section: '2.18'
- title: Configurable retry caps via BuildConfig + `gobby build` CLI
  category: code
  task_type: feature
  depends_on:
  - '1.7'
  - '3.2'
  validation_criteria: src/gobby/config/build.py, src/gobby/build/service.py, src/gobby/cli/build.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.19:2.19.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.19:2.19.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.19:2.19.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.19:2.19.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.19:2.19.5
  - covers:task-12725-lifecycle-dispatch-rev1:2.19:2.19.6
  - covers:task-12725-lifecycle-dispatch-rev1:2.19:2.19.7
  assigned_agent: backend-developer
  tdd: true
  source_section: '2.19'
- title: 'Plan-Coverage Contract: `## Task Manifest` section requirement'
  category: docs
  task_type: feature
  depends_on: []
  validation_criteria: docs/contracts/plan-coverage.md
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.3a
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.3b
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.3c
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.3d
  - covers:task-12725-lifecycle-dispatch-rev1:2.21:2.21.4
  assigned_agent: developer
  tdd: false
  source_section: '2.21'
- title: Manifest-emitter library
  category: code
  task_type: feature
  depends_on:
  - '2.21'
  validation_criteria: src/gobby/plans/manifest_emitter.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.21a:2.21a.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.21a:2.21a.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.21a:2.21a.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.21a:2.21a.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.21a:2.21a.5
  - covers:task-12725-lifecycle-dispatch-rev1:2.21a:2.21a.6
  assigned_agent: backend-developer
  tdd: true
  source_section: 2.21a
- title: 'plan-adversary agent: manifest emission on approval'
  category: config
  task_type: feature
  depends_on:
  - '2.21'
  - 2.21a
  validation_criteria: src/gobby/install/shared/workflows/agents/plan-adversary.yaml
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.22:2.22.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.22:2.22.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.22:2.22.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.22:2.22.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.22:2.22.5
  assigned_agent: developer
  tdd: false
  source_section: '2.22'
- title: 'planner agent / plan-draft skill: fresh context + tighter mandate'
  category: config
  task_type: feature
  depends_on:
  - '2.21'
  - '2.22'
  validation_criteria: src/gobby/install/shared/skills/plan-draft/SKILL.md
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.23:2.23.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.23:2.23.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.23:2.23.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.23:2.23.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.23:2.23.5
  assigned_agent: developer
  tdd: false
  source_section: '2.23'
- title: '`/gobby plan` skill: end-to-end coordinator flow'
  category: config
  task_type: feature
  depends_on:
  - '2.21'
  - '2.22'
  - '2.23'
  validation_criteria: src/gobby/install/shared/skills/plan/SKILL.md
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.5
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.6
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.7
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.8
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.9
  - covers:task-12725-lifecycle-dispatch-rev1:2.24:2.24.10
  assigned_agent: developer
  tdd: false
  source_section: '2.24'
- title: 'Re-expansion of #12725 as Epic 1 end-to-end validation'
  category: manual
  task_type: feature
  depends_on:
  - '2.21'
  - '2.22'
  validation_criteria: the run's `compiled_summary` reports correct phase_count, task_count,
    and dependency_count; logs name `deterministic` as the path used
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:2.20:2.20.1
  - covers:task-12725-lifecycle-dispatch-rev1:2.20:2.20.2
  - covers:task-12725-lifecycle-dispatch-rev1:2.20:2.20.3
  - covers:task-12725-lifecycle-dispatch-rev1:2.20:2.20.4
  - covers:task-12725-lifecycle-dispatch-rev1:2.20:2.20.5
  assigned_agent: developer
  tdd: false
  source_section: '2.20'
- title: "Build service \u2014 CLI + MCP + HTTP shared core"
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/build/service.py
  labels:
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.1
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.2
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.3
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.4
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.5
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.6
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.7
  - covers:task-12725-lifecycle-dispatch-rev1:3.2:3.2.8
  assigned_agent: backend-developer
  tdd: true
  source_section: '3.2'
```
