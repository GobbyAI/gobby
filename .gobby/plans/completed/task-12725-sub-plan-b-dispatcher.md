> **Plan ID:** sub-plan-b-dispatcher

# Sub-Plan B — Dispatcher + Interactive Surface for 0.4.0

## P1 Overview

`kind: framing`

This plan slices rev1 (`.gobby/plans/task-12725-lifecycle-dispatch-rev1.md`)
into a 0.4.0-shippable cut, adds the per-leaf interactive skill trios that
rev1 did not cover (`/gobby dev`, `/gobby qa`, `/gobby review`), introduces a
project-wide automation halt (`gobby build stop`), and renames the per-task
yolo concern into `unattended` while pinning a stub `--yolo` flag for the
post-0.4.0 composer agent. Sub-plan A (manifest-driven compile) is already
landed; this plan is the second of two slices required to expand the other
queued plans under `.gobby/plans/` autonomously.

## P2 Constraints

`kind: framing`

- **Lifecycle chain order is locked, not implemented.** The post-0.4.0 chain
  (ideate → research → architect → prd → plan → test-arch → expand → dev →
  qa → review → merge → ship) is the reference; only `dev`, `qa`, `review`,
  `merge`, and the `test-architect` agent ship in 0.4.0. The other phase
  surfaces (`ideate`, `research`, `architect`, `prd`, `test-arch` skill,
  `pm`, `ship`) defer post-0.4.0. Artifact paths for the deferred phases
  are pinned now (`.gobby/docs/research/*.md`,
  `.gobby/docs/architecture/*.md`, `.gobby/docs/prd.md`) so future trios
  drop in without rework. `/gobby research` is a multi-modal skill
  (modes selected via skill args/references — mirrors how `impeccable`
  exposes `craft`/`teach`/`extract` modes); the mode catalog is
  post-0.4.0 design.
- **No phase-topology novelty.** The dispatcher rule shape ships per rev1; no
  multi-agent loops or parallel fan-out beyond `dev↔qa`.
- **Configurability via clone, not edit.** Bundled templates ship `source:
  gobby` and refuse mutation; users override by copying to project-local
  `.gobby/install/<kind>/<name>/` paths.
- **Composer agent is stubbed.** `--yolo` is plumbed through CLI/MCP/HTTP but
  no-ops at runtime since the composer does not exist yet. Reserves the
  surface so the post-0.4.0 chain drops in without flag churn.
- **Subtree-pause is out of scope for 0.4.0.** `gobby build stop` is a
  project-wide halt (toggles the bundled dispatcher cron row off). Subtree
  pause via per-task `allow_automation` flip-and-restore is deferred (the
  ledger pattern that would make it safe is post-0.4.0). Explicit per-task
  opt-out remains available via `gobby build` itself.
- **Re-expansion of rev1 itself (§2.20) is the validation gate.** End-to-end
  on `task-12725-lifecycle-dispatch-rev1.md` walks plan → expand → dev → qa
  → holistic_review → merge cleanly under the dispatcher built here.

## P3 Phase 1 — Dispatcher Foundation

`kind: framing`

**Goal**: Land the state-driven dispatcher core (rev1 §1.3–§1.10) so any
opted-in task transitions through ordered rules with a per-task mutex and
deterministic action emission. Includes the storage-side yolo→unattended
rename (1.10) so the dispatcher helpers read a real field; the build/CLI
surface for the rename lives in Phase 3 (3.1).

### 1.1 Task model dispatch helpers [category: code] (depends: 1.10)

`kind: deliverable`

Target: `src/gobby/storage/tasks/_crud.py`

Add `_skipped_stages(labels)` and `_is_unattended(task)` per rev1 §1.3.4.
`_skipped_stages` parses `stage-:<name>` labels into a typed set;
`_is_unattended` returns the per-task automation mode (reads
`Task.unattended`, the field renamed by 1.10). Both helpers are read by the
dispatcher and the build service, so they live on the CRUD module surface
and are imported, not duplicated.

**Acceptance:**

- 1.1.1 - Skipped-stages reader exists. symbol: `gobby.storage.tasks._crud._skipped_stages`.
- 1.1.2 - Unattended-mode reader exists. symbol: `gobby.storage.tasks._crud._is_unattended`.
- 1.1.3 - Helpers covered by unit tests. test: `tests/storage/tasks/test_dispatch_helpers.py::test_skipped_stages_parses_labels`.
- 1.1.4 - Unattended helper reads the renamed field. test: `tests/storage/tasks/test_dispatch_helpers.py::test_is_unattended_reads_unattended_field`.

### 1.2 Per-task dispatch mutex storage alignment [category: code]

`kind: deliverable`

Target: `src/gobby/storage/tasks/_dispatch_mutex.py` (existing),
`src/gobby/storage/migrations/<NNN>_add_task_dispatch_mutex.py`

The storage helper already exists at
`src/gobby/storage/tasks/_dispatch_mutex.py` with `TaskDispatchMutexManager`
defined and an `ensure_table()` test helper. This deliverable extends the
existing module to satisfy the rev1 §1.4 contract: short-lived leases
keyed by task_ref, acquire/release/sweep methods, force-release escape
hatch for operator recovery, an `attach_run_id(mutex_id, run_id)` method
to link the lease to a spawned agent's run id, and the canonical
migration that creates the table for production (replacing the
`ensure_table()` test-only path). Startup sweep of expired leases runs
from the daemon initializer.

**Acceptance:**

- 1.2.1 - Manager continues to live at the existing import path. symbol: `gobby.storage.tasks._dispatch_mutex.TaskDispatchMutexManager`.
- 1.2.2 - Canonical migration creates the table. file: `src/gobby/storage/migrations/<NNN>_add_task_dispatch_mutex.py`.
- 1.2.3 - attach_run_id links a lease to a run. symbol: `gobby.storage.tasks._dispatch_mutex.TaskDispatchMutexManager.attach_run_id`.
- 1.2.4 - Acquire-release round-trips. test: `tests/storage/tasks/test_dispatch_mutex.py::test_acquire_release_round_trip`.
- 1.2.5 - Expired-lease sweep covered. test: `tests/storage/tasks/test_dispatch_mutex.py::test_sweep_expired`.
- 1.2.6 - Force-release escape hatch covered. test: `tests/storage/tasks/test_dispatch_mutex.py::test_force_release`.
- 1.2.7 - attach_run_id covered. test: `tests/storage/tasks/test_dispatch_mutex.py::test_attach_run_id_links_run_to_lease`.
- 1.2.8 - Startup sweep runs from daemon initializer. test: `tests/storage/tasks/test_dispatch_mutex.py::test_startup_sweep_clears_expired`.

### 1.2a Runtime dispatch mutex wrapper [category: code] (depends: 1.2)

`kind: deliverable`

Target: `src/gobby/dispatch/mutex.py`

Implement the runtime mutex wrapper that the dispatcher uses to acquire
and release leases via the storage helper (1.2). Provides a context
manager that:

- Acquires a lease on entry and links it to the spawned run's id via
  `attach_run_id` (1.2) once the run id is known.
- Releases on context exit (success or failure).
- Detaches cleanly on agent terminal events (1.3) without holding the
  lease beyond the agent's lifetime — exposes
  `force_release_for_run(run_id)` for the event handlers to call.
- Re-evaluates the candidate's `(lifecycle, status)` tuple after lease
  acquisition to guard against TOCTOU (1.8 wires this).

This is the dispatcher-side facade over the storage helper from 1.2;
rev1 §1.4 calls for both layers explicitly.

**Acceptance:**

- 1.2a.1 - Module exists. file: `src/gobby/dispatch/mutex.py`.
- 1.2a.2 - Context-manager class exists. symbol: `gobby.dispatch.mutex.RuntimeDispatchMutex`.
- 1.2a.3 - attach() links run id to lease. symbol: `gobby.dispatch.mutex.RuntimeDispatchMutex.attach`.
- 1.2a.4 - force_release_for_run exposed for event handlers. symbol: `gobby.dispatch.mutex.RuntimeDispatchMutex.force_release_for_run`.
- 1.2a.5 - Acquire/link/release covered. test: `tests/dispatch/test_mutex.py::test_acquire_link_release_round_trip`.
- 1.2a.6 - Detach on agent terminal does not leak lease. test: `tests/dispatch/test_mutex.py::test_detach_on_terminal_no_leak`.

### 1.3 Mutex-clearing event handlers [category: code] (depends: 1.2, 1.2a)

`kind: deliverable`

Target: `src/gobby/hooks/event_handlers/_dispatch.py`

Wire mutex release on every event that ends an agent's hold on a task —
both terminal events (agent close, crash classification, parent reopen)
and the normal claim/end-agent cleanup path (agent ran cleanly to its
own end). The expansion-run terminal handlers do double duty: they
release the mutex AND advance the task's lifecycle, because expansion
is the one action whose terminal state must be lifted into
`advance_lifecycle` from §1.7 explicitly (no agent calls
`mark_task_*` for the in-process expansion run; the dispatcher owns
both sides of the boundary). Specifically:

- `on_expansion_run_completed(task_id, expansion_run_id)` calls
  `advance_lifecycle(task_id, to_lifecycle="in_development",
  to_status="open", side_effects=PreserveExpansionRunId(...))`,
  then releases the mutex via `force_release_for_run(expansion_run_id)`.
- `on_expansion_run_failed(task_id, expansion_run_id, reason)` calls
  `advance_lifecycle(task_id, to_lifecycle="expanding",
  to_status="open",
  side_effects=ClearExpansionRunIdAndIncrementAttempts(...))` to keep
  the task in `expanding` for retry; on
  `expansion_attempts >= max_expansion_attempts` the helper instead
  emits `EscalateAction` (or unattended fallback per §1.6); then
  releases the mutex.
- `on_expansion_run_cancelled(task_id, expansion_run_id)` releases
  the mutex without advancing (cancellation is operator-driven, not
  a verdict).

Without these wirings, expansion success leaves the task at
`(expanding, open)` and the next heartbeat re-fires `expansion_rule`
(or no-ops idempotently); expansion failure never increments
`expansion_attempts`. The agent-side handlers (terminal/crashed/
reopened/normal-end/claim) only release the mutex; lifecycle for
those events is owned by the spawned agent's tool calls
(`mark_task_review_*`, `mark_task_merged`, `mark_task_pr_opened`).
Each release path calls `RuntimeDispatchMutex.force_release_for_run(run_id)`
from 1.2a so the release surface is unified across agent runs and
expansion runs. New module.

**Acceptance:**

- 1.3.1 - Module exists. file: `src/gobby/hooks/event_handlers/_dispatch.py`.
- 1.3.2 - Agent-terminal handler clears the mutex. symbol: `gobby.hooks.event_handlers._dispatch.on_agent_terminal`.
- 1.3.3 - Crash-classification handler clears the mutex. symbol: `gobby.hooks.event_handlers._dispatch.on_agent_crashed`.
- 1.3.4 - Reopen handler clears the mutex. symbol: `gobby.hooks.event_handlers._dispatch.on_task_reopened`.
- 1.3.5 - Normal end-agent path clears the mutex. symbol: `gobby.hooks.event_handlers._dispatch.on_agent_end_normal`.
- 1.3.6 - Claim release on run end. symbol: `gobby.hooks.event_handlers._dispatch.on_claim_released`.
- 1.3.7 - Expansion-run completion handler clears the mutex. symbol: `gobby.hooks.event_handlers._dispatch.on_expansion_run_completed`.
- 1.3.8 - Expansion-run failure handler clears the mutex. symbol: `gobby.hooks.event_handlers._dispatch.on_expansion_run_failed`.
- 1.3.9 - Expansion-run cancellation handler clears the mutex. symbol: `gobby.hooks.event_handlers._dispatch.on_expansion_run_cancelled`.
- 1.3.10 - Terminal handler covered. test: `tests/hooks/event_handlers/test_dispatch.py::test_terminal_clears_mutex`.
- 1.3.11 - Normal-end path covered. test: `tests/hooks/event_handlers/test_dispatch.py::test_normal_end_clears_mutex`.
- 1.3.12 - Claim release path covered. test: `tests/hooks/event_handlers/test_dispatch.py::test_claim_release_clears_mutex`.
- 1.3.13 - Expansion completion advances (expanding, open) → (in_development, open) AND releases mutex when apply created children. test: `tests/hooks/event_handlers/test_dispatch.py::test_expansion_completion_advances_lifecycle_when_apply_created_children`.
- 1.3.13a - Compile-only completion does NOT advance lifecycle (only releases mutex). test: `tests/hooks/event_handlers/test_dispatch.py::test_compile_only_completion_does_not_advance_lifecycle`.
- 1.3.14 - Expansion failure stays at (expanding, open), increments expansion_attempts, AND releases mutex. test: `tests/hooks/event_handlers/test_dispatch.py::test_expansion_failure_increments_attempts_and_releases_mutex`.
- 1.3.14a - Expansion failure on max_expansion_attempts emits EscalateAction (or unattended fallback). test: `tests/hooks/event_handlers/test_dispatch.py::test_expansion_failure_on_exhaust_escalates_or_falls_back`.
- 1.3.15 - Expansion cancellation releases mutex without advancing. test: `tests/hooks/event_handlers/test_dispatch.py::test_expansion_cancellation_releases_mutex_without_advance`.
- 1.3.16 - Idempotent: expansion_rule does not re-fire after handler advances lifecycle. test: `tests/hooks/event_handlers/test_dispatch.py::test_expansion_rule_does_not_refire_after_handler_advances`.

### 1.4 Dispatch action types [category: code]

`kind: deliverable`

Target: `src/gobby/dispatch/actions.py`

Define the union of dispatcher emissions per rev1 §1.6 as frozen
dataclasses: `SpawnAgentAction`, `StartExpansionAction`,
`CreateIsolationAction`, `AdvanceLifecycleAction`,
`AppendAuditMarkerAction`, `EscalateAction`. The rule evaluator
returns one of these (or `None`). `AdvanceLifecycleAction` carries
both lifecycle stages AND status — the dispatcher's executor
forwards both into `advance_lifecycle(task_id, to_lifecycle,
to_status, side_effects)` from §1.7. Without status on the action,
skip-stage advances (e.g., qa skip → `(holistic_review,
review_approved)`), leaf-parking (`(in_development, review_approved)`
→ `(holistic_review, review_approved)`), and pr-skip
(`(pr, open)` → `(merging, open)`) cannot encode their required
status component. New module.

**Acceptance:**

- 1.4.1 - Module exists. file: `src/gobby/dispatch/actions.py`.
- 1.4.2 - Action union exported. symbol: `gobby.dispatch.actions.Action`.
- 1.4.3 - Spawn action carries agent + task refs. symbol: `gobby.dispatch.actions.SpawnAgentAction`.
- 1.4.4 - Lifecycle-advance action carries from/to (lifecycle, status) tuples. symbol: `gobby.dispatch.actions.AdvanceLifecycleAction`.
- 1.4.5 - AdvanceLifecycleAction has from_lifecycle, from_status, to_lifecycle, to_status fields. test: `tests/dispatch/test_actions.py::test_advance_action_carries_status_fields`.
- 1.4.6 - Action serialization round-trips for audit. test: `tests/dispatch/test_actions.py::test_action_round_trip`.

### 1.5 Prompt-builder registry [category: code] (depends: 1.4)

`kind: deliverable`

Target: `src/gobby/dispatch/prompts.py`

Implement the `PROMPT_BUILDERS: dict[str, PromptBuilder]` registry from rev1
§1.6a. Keys are agent slugs; values are pure callables `(task, context) ->
str`. The dispatcher reads this to hydrate `SpawnAgentAction` without
inlining prompt strings into rules. New module.

**Acceptance:**

- 1.5.1 - Module exists. file: `src/gobby/dispatch/prompts.py`.
- 1.5.2 - Registry keyed by agent slug. symbol: `gobby.dispatch.prompts.PROMPT_BUILDERS`.
- 1.5.3 - Builder type alias exported. symbol: `gobby.dispatch.prompts.PromptBuilder`.
- 1.5.4 - Each registered builder is covered. test: `tests/dispatch/test_prompts.py::test_all_registered_builders_callable`.

### 1.6 Ordered decision rules [category: code] (depends: 1.1, 1.4, 1.5)

`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`

Implement the ordered rule list per rev1 §1.7. Each rule is a pure
function that inspects the task's `(lifecycle, status)` tuple plus
task_type, labels, artifacts, and dependencies, returning either an
`Action` (1.4) or `None`. Order encodes priority — first match wins. The
evaluator is pure (no I/O); the dispatcher (1.8) invokes side effects.

**Ownership split:** §1.6 implements and registers ten of the
eleven rules below — every rule EXCEPT `merge_rule`. The
infrastructure (`evaluate` function, action emission, skip
semantics, unattended fallback, retry-cap reads) ships in §1.6
along with a stable export `BASE_RULES: list[Rule]` containing the
ten non-merge rules in order. §2.10 owns `merge_rule`'s
implementation and the final
`RULES = [*BASE_RULES, merge_rule]` export at the bottom of
`src/gobby/dispatch/rules.py`. The dispatcher (1.8) imports
`RULES` (the merged list); no consumer imports `BASE_RULES`. This
keeps the durable invariant testable in both modules: §1.6 asserts
`BASE_RULES` has exactly ten entries and excludes `merge_rule`;
§2.10 asserts `RULES == [*BASE_RULES, merge_rule]` and that the
final list has eleven entries with `merge_rule` at position 11.
No deliverable writes to `gobby.dispatch.rules.merge_rule` except
§2.10.

**Rule roster** (the explicit ordered list — every entry must be
implemented; the dispatcher relies on first-match semantics across all
eleven):

- **(1) plan_review_rule** — fires on `(plan_review, open)` when ALL
  of: (a) the task has `task_artifacts.plan_file_path`, (b) label
  `stage-:plan_review` is absent, AND (c) the await-revision guard
  is clear — meaning either no prior rejection (no
  `task_artifacts.last_reviewed_plan_hash`) OR
  `plan_hash != task_artifacts.last_reviewed_plan_hash` (the plan
  file has changed since the last rejection). Emits
  `SpawnAgentAction(plan-adversary)`. Cap source:
  `BuildConfig.max_review_rounds`. `(open, open)` is reserved for
  pre-build/backlog state and never re-entered post-build (see 1.7's
  matrix); plan-file builds enter directly at `(plan_review, open)`.

  **Why the guard:** without it, after a plan rejection the task
  stays at `(plan_review, open)` and the rule re-fires every
  heartbeat against the unchanged file until `max_review_rounds` is
  exhausted, burning rounds on no new content. The guard requires a
  human (or revision agent — out of 0.4.0 scope) to actually edit
  the plan and bump `plan_hash` before the next adversary spawns.
  Plan rejection in §1.7's matrix writes
  `task_artifacts.last_reviewed_plan_hash = current plan_hash` as a
  side effect, so the rule can compare against it on the next
  heartbeat.
- **(2) test_arch_rule** — fires on `(test_arch, open)`. If
  `stage-:test_arch` is set, emits `AdvanceLifecycleAction → (expanding,
  open)` instead of dispatching the agent. Otherwise emits
  `SpawnAgentAction(test-architect)`. Approval advances to
  `(expanding, open)` per the transition matrix in 1.7.
- **(3) expansion_rule** — fires on `(expanding, open)` when
  `stage-:expanding` is absent. Emits `StartExpansionAction`. Cap
  source: `BuildConfig.max_expansion_attempts`.
- **(4) isolation_rule** — fires on `(in_development, open)` for
  leaves (`task_type == "task"`) when the requested isolation mode
  on the task itself is `"worktree"` or `"clone"` (read via
  `task.isolation` — the column on the `Task` model, NOT a
  `task_artifacts` field) AND the corresponding produced artifact
  pair on `task_artifacts` is absent (`worktree_path` /
  `worktree_id` for worktree; `clone_path` / `clone_id` for clone).
  `task_artifacts` carries the produced isolation pair only; the
  requested mode lives on the task. Emits `CreateIsolationAction`.
  Returns control to the candidate loop on the next heartbeat;
  `dev_rule` (rule 5) only fires once the artifact pair is present,
  guaranteeing a developer never spawns without isolation. When
  `task.isolation == "none"` (or unset), the rule short-circuits
  with `None` so dev_rule fires immediately. No retry cap;
  persistent isolation-creation failure surfaces as `EscalateAction`
  from `CreateIsolationAction`'s executor (1.8).
- **(5) dev_rule** — fires on `(in_development, open)` for leaves
  (`task_type == "task"`) when `is_blocked_by_deps(task)` is `False`
  (1.8 helper), `assigned_agent` is set on the task, AND any
  requested isolation artifact pair from rule 4 is present. Emits
  `SpawnAgentAction(assigned_agent)`. No per-leaf cap; rejection
  re-fires on the next heartbeat.
- **(6) qa_rule** — fires on `(in_development, needs_review)` for
  leaves. If `stage-:qa` is set, emits `AdvanceLifecycleAction →
  (holistic_review, review_approved)` (skip-then-park). Otherwise
  `SpawnAgentAction(qa-reviewer)`. Cap source:
  `BuildConfig.max_qa_rounds`.
- **(7) leaf_park_rule** — fires on `(in_development, review_approved)`
  for leaves. Emits `AdvanceLifecycleAction → (holistic_review,
  review_approved)`. This is the bookkeeping advance that surfaces a
  parked leaf in the kanban's holistic_review column without dispatching
  any agent.
- **(8) all_leaves_holistic_rule** — fires on epics at
  `(in_development, open)` (`task_type == "epic"`) when every
  child leaf is in a terminal-or-parked state — closed, escalated,
  parked at `(holistic_review, review_approved)`, or already
  `(merged, closed)`. Without this rule, the parent epic gets
  stranded at `(in_development, open)` after every leaf finishes
  QA: `leaf_park_rule` only moves leaves; `holistic_rule` only
  fires on epics already at `(holistic_review, open)`. Emits
  `AdvanceLifecycleAction → (holistic_review, open)`. The
  documented skip semantics still apply: under
  `stage-:holistic_review` the rule still emits the holistic_review
  advance (the holistic_rule's skip path then takes over to advance
  past holistic to `(pr, open)`); the rule never bypasses the
  non-skippable merge stage. No retry cap; this is a one-shot
  bookkeeping advance triggered by the cascade of leaf approvals.
- **(9) holistic_rule** — fires on `(holistic_review, open)` for
  epics (`task_type == "epic"`) when every child leaf is parked at
  `(holistic_review, review_approved)` (or terminal-equivalent) and
  `stage-:holistic_review` is absent. Emits
  `SpawnAgentAction(holistic-reviewer)`. Cap source:
  `BuildConfig.max_holistic_rounds`. Rejection requires
  `cited_subtasks` (1.7).
- **(10) pr_rule** — fires on `(pr, open)` for epics when
  `stage-:pr` is absent. PR creation is a human-or-CI act in the
  0.4.0 cut; no PR-creation agent ships in this plan. Behavior:
  - **Attended (`task.unattended == False`)** → emit
    `EscalateAction` with reason `pr_creation_required` and a
    structured payload pointing at the worktree/clone branch and
    target merge base, surfacing it to the user (or operator) to
    open the PR by hand. The escalation moves the task to
    `(pr, escalated)` (per §1.8's `EscalateAction` executor). When
    the operator opens the PR, they call
    `mark_task_pr_opened(task_id, pr_url)` from §1.7 / §2.9, which
    accepts BOTH `(pr, open)` and `(pr, escalated)` as from-states:
    it sets `task_artifacts.pr_url`, atomically clears the
    escalation metadata (`is_escalated=False`, escalation reason
    cleared), and advances to `(pr, needs_review)`. External review
    approval (via the existing `mark_task_review_approved`) then
    advances to `(merging, open)`.
  - **Unattended (`task.unattended == True`)** → emit
    `AdvanceLifecycleAction → (merging, open)` deterministically;
    `task_artifacts.pr_url` is left unset (the merge agent's
    write-back tools accept a missing pr_url under unattended
    fallback per 2.9). This is the same target as the `stage-:pr`
    skip-stage advance.
  - **Cap source:** none. The escalation path returns control to
    the operator; the unattended advance is one-shot.
  Real PR-opening tooling (and a `pr-opener` or `merge-orchestrator`
  contract that covers PR creation) is tracked outside this plan
  (rev1 §1.7 / #12728).
- **(11) merge_rule** — IMPLEMENTED IN §2.10 (not §1.6). Reserved
  position 11 in the `RULES` list; §2.10 appends. Contract: fires on
  `(merging, open)` for epics, emits
  `SpawnAgentAction(merge-orchestrator)`, does NOT advance lifecycle
  (merge agent owns terminal write-back via `mark_task_merged` /
  `mark_task_merge_failed` from 1.7), cap source
  `BuildConfig.max_merge_attempts`. §1.6 documents the contract here
  for ordering; §2.10 owns the body and tests.

**Skip semantics:** every rule with a corresponding `stage-:<name>` label
short-circuits to `AdvanceLifecycleAction` advancing past that stage (or
to the appropriate parking state) instead of dispatching the agent. The
stages with skip support and their exact skip targets are:

- `stage-:plan_review` on `(plan_review, open)` →
  `AdvanceLifecycleAction → (test_arch, open)` (skip plan adversary as
  if approved). Only valid when `task_artifacts.plan_file_path` is set
  AND no plan_review label has previously rejected the plan; the build
  service rejects the combination at build time otherwise.
- `stage-:test_arch` on `(test_arch, open)` →
  `AdvanceLifecycleAction → (expanding, open)` (skip test architect).
- `stage-:expanding` on `(expanding, open)` →
  `AdvanceLifecycleAction → (in_development, open)` (skip expansion).
  Only valid when the task already has children OR
  `task_type == "task"` (leaf entry); the build service rejects the
  combination at build time otherwise.
- `stage-:qa` on `(in_development, needs_review)` →
  `AdvanceLifecycleAction → (holistic_review, review_approved)`
  (skip-then-park; same target as qa-approval).
- `stage-:holistic_review` on `(holistic_review, open)` →
  `AdvanceLifecycleAction → (pr, open)` (skip holistic reviewer as if
  approved). Only valid when every child leaf is parked at
  `(holistic_review, review_approved)`.
- `stage-:pr` on `(pr, open)` →
  `AdvanceLifecycleAction → (merging, open)` (skip the PR-creation
  escalation; deterministic advance to merge). Only valid in
  conjunction with `task.unattended == True`; the build service
  rejects attended `stage-:pr` because skipping human PR creation
  silently is not a deterministic fallback.

Stages without skip support: `(in_development, open)` (dev) — dev
cannot be skipped; if you don't want development on a leaf, do not
build it; `(merging, open)` (merge) — merge cannot be skipped because
no terminal-state writeback occurs without it.

The build service validates the `stage-:<name>` label set against the
task's lifecycle, type, and `unattended` flag at build time, rejecting
invalid combinations with a structured error rather than letting them
park forever in the dispatcher.

**Unattended fallback:** the rules read the unattended flag through
the `_is_unattended(task)` helper from §1.1 (which reads
`Task.unattended` after the §1.10 storage rename has landed); rules
do not read `task.unattended` directly. When `_is_unattended(task)`
is `True` and a rule would normally escalate (max retry reached,
ambiguous routing), emit `AdvanceLifecycleAction` to the next
deterministic stage with an `AppendAuditMarkerAction` recording the
auto-fallback. When `_is_unattended(task)` is `False`, emit
`EscalateAction` with a reason naming the rule and the failure mode
(e.g., `qa_rejected:max_attempts`). The helper indirection is
deliberate: it keeps §1.6's implementation order-independent of
§1.10's column rename — §1.6's transitive dependency chain is
1.6 → 1.1 → 1.10, so the rules cannot land before the column
exists.

**Retry-cap read path:** rules read caps from `BuildConfig` (loaded by
the dispatcher and threaded through evaluator context). Counters are
tracked on `task_artifacts` (e.g., `qa_attempts`, `holistic_attempts`,
`expansion_attempts`, `merge_attempts`) and incremented by the
corresponding rejection-handling code in 1.7's transition matrix.

**Acceptance:**

- 1.6.1 - Module exists. file: `src/gobby/dispatch/rules.py`.
- 1.6.2 - BASE_RULES exported with ten entries (merge_rule appended by 2.10 to form final RULES). symbol: `gobby.dispatch.rules.BASE_RULES`.
- 1.6.3 - First-match-wins evaluator exists. symbol: `gobby.dispatch.rules.evaluate`.
- 1.6.4 - plan_review_rule fires on (plan_review, open) with plan_file_path. test: `tests/dispatch/test_rules.py::test_plan_review_rule_fires_on_plan_review_tuple`.
- 1.6.5 - test_arch_rule fires on (test_arch, open) and skip advances. test: `tests/dispatch/test_rules.py::test_test_arch_rule_fires_and_skip_advances`.
- 1.6.6 - expansion_rule fires on (expanding, open) and respects max_expansion_attempts. test: `tests/dispatch/test_rules.py::test_expansion_rule_fires_with_cap`.
- 1.6.7 - isolation_rule reads task.isolation (not task_artifacts.isolation) and fires when produced pair absent. test: `tests/dispatch/test_rules.py::test_isolation_rule_reads_task_isolation_field_and_fires_when_pair_missing`.
- 1.6.8 - isolation_rule short-circuits when task.isolation == none. test: `tests/dispatch/test_rules.py::test_isolation_rule_skips_when_task_isolation_none`.
- 1.6.9 - dev_rule does not fire while requested isolation artifacts are absent. test: `tests/dispatch/test_rules.py::test_dev_rule_blocked_by_missing_isolation_artifacts`.
- 1.6.10 - dev_rule fires on unblocked leaves with assigned_agent and present isolation artifacts. test: `tests/dispatch/test_rules.py::test_dev_rule_fires_on_unblocked_leaves`.
- 1.6.11 - qa_rule fires on (in_development, needs_review) and respects max_qa_rounds. test: `tests/dispatch/test_rules.py::test_qa_rule_fires_with_cap`.
- 1.6.12 - leaf_park_rule advances approved leaves to parking state. test: `tests/dispatch/test_rules.py::test_leaf_park_rule_advances_to_holistic_parking`.
- 1.6.12a - all_leaves_holistic_rule advances epic (in_development, open) → (holistic_review, open) when every leaf is parked or terminal. test: `tests/dispatch/test_rules.py::test_all_leaves_holistic_rule_advances_epic_when_leaves_parked`.
- 1.6.12b - all_leaves_holistic_rule does NOT fire while any leaf is still in flight. test: `tests/dispatch/test_rules.py::test_all_leaves_holistic_rule_holds_while_leaves_in_flight`.
- 1.6.12c - all_leaves_holistic_rule does not bypass the non-skippable merge stage. test: `tests/dispatch/test_rules.py::test_all_leaves_holistic_rule_never_targets_merging_directly`.
- 1.6.13 - holistic_rule fires on epics with all leaves parked. test: `tests/dispatch/test_rules.py::test_holistic_rule_fires_when_leaves_parked`.
- 1.6.14 - pr_rule attended escalates with reason pr_creation_required. test: `tests/dispatch/test_rules.py::test_pr_rule_attended_escalates_for_pr_creation`.
- 1.6.14a - pr_rule unattended advances (pr, open) → (merging, open). test: `tests/dispatch/test_rules.py::test_pr_rule_unattended_advances_to_merging`.
- 1.6.15 - stage-:plan_review skip advances to (test_arch, open). test: `tests/dispatch/test_rules.py::test_stage_skip_plan_review_advances_to_test_arch`.
- 1.6.16 - stage-:test_arch skip advances to (expanding, open). test: `tests/dispatch/test_rules.py::test_stage_skip_test_arch_advances_to_expanding`.
- 1.6.17 - stage-:expanding skip advances to (in_development, open). test: `tests/dispatch/test_rules.py::test_stage_skip_expanding_advances_to_in_development`.
- 1.6.18 - stage-:qa skip advances to (holistic_review, review_approved). test: `tests/dispatch/test_rules.py::test_stage_skip_qa_advances_to_holistic_parking`.
- 1.6.19 - stage-:holistic_review skip advances to (pr, open). test: `tests/dispatch/test_rules.py::test_stage_skip_holistic_advances_to_pr`.
- 1.6.20 - stage-:pr skip advances to (merging, open) under unattended only. test: `tests/dispatch/test_rules.py::test_stage_skip_pr_advances_to_merging_under_unattended`.
- 1.6.21 - Build service rejects invalid skip-stage combinations at build time. test: `tests/build/test_skip_stage_validation.py::test_invalid_skip_combinations_rejected_at_build_time`.
- 1.6.22 - Unattended fallback advances on max retries. test: `tests/dispatch/test_rules.py::test_unattended_advances_on_max_retries`.
- 1.6.23 - Attended fallback escalates with reason. test: `tests/dispatch/test_rules.py::test_attended_escalates_with_reason`.
- 1.6.24 - BASE_RULES exported with exactly the ten non-merge rules in order, includes all_leaves_holistic_rule at position 8, excludes merge_rule. test: `tests/dispatch/test_rules.py::test_base_rules_has_ten_entries_with_all_leaves_holistic_at_position_8_and_excludes_merge_rule`.

### 1.7 Lifecycle transitions in review tools [category: code]

`kind: deliverable`

Target: `src/gobby/storage/tasks/_transitions.py`,
`src/gobby/storage/migrations/<NNN>_add_last_reviewed_plan_hash.py`

Per rev1 §1.8, formalize the per-task `(lifecycle, status)` state machine
that the dispatcher (1.6) routes on. The deliverable adds a single
`advance_lifecycle(task_id, to_lifecycle, to_status, side_effects)`
helper that performs every transition, writes the
`task_lifecycle_events` audit row, and applies the side-effect bundle
(counter clears, artifact resets, cascade close). Existing
`mark_task_review_approved` (`_transitions.py:216`) and
`mark_task_review_rejected` (`_transitions.py:242`) call through to this
helper. Three new transition helpers ship alongside —
`mark_task_merged(task_id, pr_url=None, merge_sha=None)`,
`mark_task_merge_failed(task_id, reason)`, and
`mark_task_pr_opened(task_id, pr_url)` — so callers (the merge
agent's YAML allowlist for the merge helpers; the operator
performing a human PR-open for `mark_task_pr_opened`) have
constrained, named transition surfaces and never need direct access
to `advance_lifecycle`. `mark_task_pr_opened` is the writer for both PR-stage from-states
that lead to the wait state — `(pr, open) → (pr, needs_review)`
AND `(pr, escalated) → (pr, needs_review)`. The escalated case
is the recovery path for the attended-pr_rule escalation
(`reason: pr_creation_required`): the operator opens the PR and
calls `mark_task_pr_opened(task_id, pr_url)`, which atomically
(a) sets `task_artifacts.pr_url`, (b) advances status to
`needs_review`, (c) clears the task's escalation metadata
(`is_escalated=False`, escalation reason cleared), and (d) writes
the lifecycle event. It is required because the existing
`mark_task_needs_review(task_id, review_notes)` tool does not
accept `pr_url` and cannot clear escalation, so it cannot drive
the PR wait state from either from-state. The gobby-tasks MCP registration for all three
tools lands in §2.9 (which already registers
`mark_task_review_approved` / `mark_task_review_rejected`). Both `pr_url` and `merge_sha` are optional because
the existing `merge_worktree` / `merge_clone` tools do not return a
merge SHA in the 0.4.0 cut (rev1 explicitly defers
`merge_commit_sha` to #12728), and unattended/skipped-PR paths can
reach merge without a `pr_url`. The helper writes whatever artifacts
the agent supplies; absent values leave the corresponding
`task_artifacts` columns NULL with no error. `de_escalate_task` is extended to accept an explicit
target `(lifecycle, status)` rather than always returning to
`(open, open)`.

**`(open, open)` is reserved for pre-build/backlog state.** No
transition in the matrix below routes back to it post-build; build
entry seeds the appropriate post-`open` lifecycle stage and rejection
paths route to a named lifecycle tuple (typically `(plan_review,
open)`) so the kanban column stays accurate and the dispatcher's
plan_review_rule (1.6) re-fires cleanly.

**Transition matrix** — every `(from-tuple, verdict)` an expansion agent
must implement, with side effects:

- `(plan_review, open)` + plan approved → `(test_arch, open)`. Clear
  `task_artifacts.plan_review_attempts`.
- `(plan_review, open)` + plan rejected → `(plan_review, open)` await-
  revision; set `task_artifacts.last_reviewed_plan_hash = plan_hash`
  (the just-rejected hash); increment `plan_review_attempts`; on
  `>= max_review_rounds`, emit `EscalateAction` (or unattended
  fallback per 1.6). The task stays in the `plan_review` kanban
  column. plan_review_rule (1.6) refuses to re-dispatch until the
  plan file is edited and `plan_hash` changes, so the next adversary
  round only spawns when there is actually new content to review.
- `(test_arch, open)` + approved → `(expanding, open)`. Clear
  `test_arch_attempts`.
- `(test_arch, open)` + rejected → `(plan_review, open)`. Increment
  `test_arch_attempts`; the test architect's verdict is that the plan
  needs revision, so the task returns to the plan_review column for
  another adversary pass — not back to `(open, open)`.
- `(expanding, open)` + success → `(in_development, open)`. Preserve
  `task_artifacts.expansion_run_id` for diagnostics.
- `(expanding, open)` + failure → `(expanding, open)` retry; clear
  `task_artifacts.expansion_run_id`; increment `expansion_attempts`;
  on `>= max_expansion_attempts`, emit `EscalateAction` (or unattended
  fallback per 1.6). Task stays in `expanding` until escalation; on
  unattended fallback, a deterministic escape route is to advance to
  `(plan_review, open)` for plan revision rather than back to
  `(open, open)`.
- `(in_development, open)` (leaf) + dev complete →
  `(in_development, needs_review)`.
- `(in_development, needs_review)` (leaf) + qa approved →
  `(holistic_review, review_approved)`. **LEAF PARKING STATE.**
- `(in_development, needs_review)` (leaf) + qa rejected →
  `(in_development, open)`. Increment `qa_attempts`; clear claim.
- `(in_development, open)` (epic) + all leaves parked or terminal
  (closed / escalated / `(holistic_review, review_approved)` /
  `(merged, closed)`) → `(holistic_review, open)`. Bookkeeping
  advance triggered by `all_leaves_holistic_rule` (§1.6 rule 8);
  no agent dispatched. The holistic_rule (rule 9) fires on the
  next heartbeat to either spawn the holistic-reviewer or take
  the `stage-:holistic_review` skip path to `(pr, open)`.
- `(holistic_review, open)` (epic) + approved → `(pr, open)`.
- `(holistic_review, open)` (epic) + rejected with `cited_subtasks` →
  named cited leaves reset to `(in_development, open)`; un-cited leaves
  stay parked at `(holistic_review, review_approved)`; epic stays at
  `(holistic_review, open)` for re-review. Increment
  `holistic_attempts`.
- `(pr, open)` + PR opened (internally or via human escalation) →
  `(pr, needs_review)`. Set `task_artifacts.pr_url`. The wait state
  uses the existing `needs_review` status enum value, NOT a new
  `claimed` status (which is not part of the task status enum:
  `open`, `in_progress`, `needs_review`, `review_approved`, `closed`,
  `escalated`). Claim-ownership tracking is separate metadata on the
  task and is unrelated to lifecycle status.
- `(pr, needs_review)` + external approval → `(merging, open)`. The
  external-approval call goes through `mark_task_review_approved`,
  which `advance_lifecycle` routes for the `pr` lifecycle stage.
- `(merging, open)` + clean merge → `(merged, closed)`. Merge agent
  writes the terminal lifecycle and triggers cascade-close on the
  subtree (every child leaf transitions to `(merged, closed)`
  bookkeeping; leaves do not run a per-leaf merge).
- `(merging, open)` + conflict (unattended) → `(merging, open)` retry;
  increment `merge_attempts`; on exhaust → unattended fallback per 1.6.
- `(merging, open)` + conflict (attended) → escalated with reason
  `merge_failed:max_attempts`.

**Validation gate sets** — the validation harness reads the
`(lifecycle, status)` tuple to decide which gate set runs.
`(in_development, needs_review)` runs full QA gates;
`(holistic_review, open)` runs cross-leaf coherence gates;
`(merging, open)` runs merge-readiness gates. Implementer enumerates the
gate-per-tuple mapping in `tests/tasks/test_validation_gates.py` and
asserts each tuple's expected gate set.

**Acceptance:**

- 1.7.1 - advance_lifecycle helper exists. symbol: `gobby.storage.tasks._transitions.advance_lifecycle`.
- 1.7.2 - mark_task_merged helper exists. symbol: `gobby.storage.tasks._transitions.mark_task_merged`.
- 1.7.2a - mark_task_pr_opened helper exists. symbol: `gobby.storage.tasks._transitions.mark_task_pr_opened`.
- 1.7.3 - mark_task_merge_failed helper exists. symbol: `gobby.storage.tasks._transitions.mark_task_merge_failed`.
- 1.7.4 - Extended de_escalate_task accepts target lifecycle/status. symbol: `gobby.storage.tasks._transitions.de_escalate_task`.
- 1.7.4a - task_artifacts.last_reviewed_plan_hash column added via migration. file: `src/gobby/storage/migrations/<NNN>_add_last_reviewed_plan_hash.py`.
- 1.7.4b - Migration round-trips. test: `tests/storage/migrations/test_add_last_reviewed_plan_hash.py::test_round_trip`.
- 1.7.5 - mark_task_review_approved calls advance_lifecycle. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_approve_calls_advance_lifecycle`.
- 1.7.6 - mark_task_review_rejected calls advance_lifecycle with reset side effects. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_reject_calls_advance_lifecycle`.
- 1.7.7 - Plan approved advances (plan_review, open) → (test_arch, open) with counter clear. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_plan_approved_advances_to_test_arch`.
- 1.7.8 - Plan rejected stays at (plan_review, open), sets last_reviewed_plan_hash, and increments attempts. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_plan_rejected_sets_last_reviewed_hash_and_increments_attempts`.
- 1.7.8a - plan_review_rule does not re-dispatch on unchanged plan hash. test: `tests/dispatch/test_rules.py::test_plan_review_rule_suppressed_when_hash_unchanged_after_rejection`.
- 1.7.8b - plan_review_rule re-dispatches when plan hash changes after rejection. test: `tests/dispatch/test_rules.py::test_plan_review_rule_redispatches_on_changed_hash`.
- 1.7.9 - Test-arch approved advances test_arch → expanding. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_test_arch_approved_advances`.
- 1.7.10 - Test-arch rejection routes back to (plan_review, open), not (open, open). test: `tests/storage/tasks/test_transitions_lifecycle.py::test_test_arch_rejected_routes_to_plan_review`.
- 1.7.11 - No matrix transition produces (open, open) as a destination post-build. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_no_post_build_transition_targets_open_open`.
- 1.7.12 - Expansion success preserves expansion_run_id. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_expansion_success_preserves_run_id`.
- 1.7.13 - Expansion failure stays at (expanding, open) and increments attempts. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_expansion_failure_stays_in_expanding_and_increments_attempts`.
- 1.7.14 - Dev-complete moves leaf to needs_review. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_dev_complete_transitions_leaf_to_needs_review`.
- 1.7.15 - QA approval parks leaf at (holistic_review, review_approved). test: `tests/storage/tasks/test_transitions_lifecycle.py::test_qa_approval_parks_leaf`.
- 1.7.16 - QA rejection resets leaf and increments qa_attempts. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_qa_rejection_resets_leaf`.
- 1.7.16a - Epic (in_development, open) advances to (holistic_review, open) when all leaves are parked-or-terminal. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_epic_advances_to_holistic_when_all_leaves_parked_or_terminal`.
- 1.7.17 - Holistic approval advances epic to pr. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_holistic_approved_advances_to_pr`.
- 1.7.18 - Holistic rejection with cited_subtasks resets only cited leaves. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_holistic_rejection_resets_only_cited_leaves`.
- 1.7.19 - mark_task_pr_opened advances (pr, open) → (pr, needs_review) with pr_url. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_pr_opened_from_open`.
- 1.7.19a - mark_task_pr_opened advances (pr, escalated) → (pr, needs_review) with pr_url AND clears is_escalated. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_pr_opened_from_escalated_clears_escalation`.
- 1.7.19b - PR external approval transitions (pr, needs_review) → (merging, open). test: `tests/storage/tasks/test_transitions_lifecycle.py::test_pr_external_approval_advances_to_merging`.
- 1.7.19c - No transition produces a non-enum status (e.g. claimed). test: `tests/storage/tasks/test_transitions_lifecycle.py::test_no_transition_produces_non_enum_status`.
- 1.7.20 - mark_task_merged with both pr_url and merge_sha sets (merged, closed) and writes both artifacts. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_merged_with_pr_url_and_merge_sha_writes_both_artifacts`.
- 1.7.20a - mark_task_merged with only pr_url succeeds; merge_commit_sha left NULL. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_merged_pr_url_only_leaves_merge_sha_null`.
- 1.7.20b - mark_task_merged with neither pr_url nor merge_sha succeeds (unattended/skipped-PR path). test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_merged_no_artifacts_succeeds_for_unattended_path`.
- 1.7.20c - mark_task_merged triggers cascade-close on the subtree. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_merged_cascades_close_on_subtree`.
- 1.7.21 - mark_task_merge_failed retries unattended within max_merge_attempts. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_merge_failed_unattended_retries`.
- 1.7.22 - mark_task_merge_failed attended escalates. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_mark_task_merge_failed_attended_escalates`.
- 1.7.23 - Each transition writes a task_lifecycle_events audit row. test: `tests/storage/tasks/test_transitions_lifecycle.py::test_audit_row_per_transition`.
- 1.7.24 - Validation gate set per (lifecycle, status) tuple. test: `tests/tasks/test_validation_gates.py::test_gate_set_per_lifecycle_status_tuple`.

### 1.8 Dispatcher scanner [category: code] (depends: 1.2, 1.2a, 1.6)

`kind: deliverable`

Target: `src/gobby/dispatch/dispatcher.py`,
`src/gobby/storage/tasks/_crud.py` (extend with candidate query helpers)

Implement the heartbeat scanner from rev1 §1.9. The scanner:

1. Calls `list_automation_candidates(db)` (new helper on the tasks
   module) to retrieve tasks meeting all of:
   - `allow_automation == True`
   - `is_claimed == False`
   - `lifecycle != merged` (terminal)
   - Not currently leased in `task_dispatch_mutex` (1.2)
   - `is_blocked_by_deps(task) is False` — no blocking dependencies in
     a non-terminal lifecycle stage.
2. For each candidate, acquires a `RuntimeDispatchMutex` (1.2a) lease,
   then RE-EVALUATES the candidate's `(lifecycle, status)` tuple inside
   the lease as a TOCTOU guard. If the tuple changed between the
   unlocked candidate scan and the locked re-check, the scanner skips
   without firing any rule.
3. Evaluates rules from 1.6 in order; on first match, executes the
   action.
4. On `SpawnAgentAction` / `StartExpansionAction`, links the resulting
   run id to the lease via `attach_run_id` (1.2) so terminal events can
   release the lease cleanly.
5. Honors a global `max_active_agents` cap (default 10) — short-circuits
   the candidate loop when the cap is reached.

**Action-kind dispatch behavior at the executor level:**

- `SpawnAgentAction` → launches the agent; lease tied to run_id; lease
  released on terminal/normal-end events (1.3).
- `StartExpansionAction` → the dispatcher executor allocates the
  expansion run id BEFORE starting any expansion work, attaches that
  run id to the dispatch mutex via `attach_run_id` FIRST, and ONLY
  THEN calls `gobby.mcp_proxy.tools.tasks._expansion.start_expansion_run_impl(...)`
  from §2.2 with the dispatcher's service container. **The
  dispatcher pins `auto_apply=True` on every dispatcher-triggered
  call**, regardless of the impl's default value. Compile-only runs
  produce no children and no `covers:` labels; if the dispatcher
  ever ran one, §1.3's expansion-completion handler would advance
  the parent to `(in_development, open)` against an empty subtree
  and `dev_rule` would never fire. Pinning `auto_apply=True` makes
  the dispatcher path equivalent to the existing MCP-tool default
  (which is also `True`) and makes the lifecycle-advance path
  deterministic. Forwarded args: task_manager, llm_service, config,
  completion_registry, triggering_session_id, task_id,
  run_id=allocated_id, **auto_apply=True**, plus optional plan_file
  / force_new / provider / model / project as required by the
  candidate. The dispatcher and MCP closure share this same impl —
  no second `ExpansionService.start_run(...)` wrapper exists.

  Defense-in-depth: §1.3's `on_expansion_run_completed` handler
  refuses to call `advance_lifecycle((expanding, open) →
  (in_development, open))` if the run completed in compile-only
  mode (i.e., no leaves were applied). Such a run is treated as a
  no-op for lifecycle purposes; the next heartbeat re-fires
  `expansion_rule` with whatever auto_apply / config the build
  produced. This guard is independent of the auto_apply pin and
  catches any future caller that doesn't pin it. This ordering closes the terminal-before-attach race:
  even if the in-process run reaches a terminal state synchronously
  inside `start_expansion_run_impl` (fast validation failure, dev-only
  expansion, immediate cancellation), the §1.3 terminal handlers
  always find the lease keyed by `expansion_run_id` because attach
  precedes any work that could fire a terminal event. The release
  path is event-driven and lives in §1.3:
  `on_expansion_run_completed` / `on_expansion_run_failed` /
  `on_expansion_run_cancelled` each call
  `RuntimeDispatchMutex.force_release_for_run(expansion_run_id)`. As
  defense-in-depth, the §1.3 handlers also accept release-by-task-id
  if release-by-run-id finds no lease — covering any future terminal
  path that bypasses run-id propagation. This symmetry with
  `SpawnAgentAction` guarantees no failed/cancelled expansion run
  strands the lease until TTL/sweep.
- `AdvanceLifecycleAction` → calls `advance_lifecycle` (1.7); releases
  the lease immediately (no agent attached). Includes leaf_park_rule's
  bookkeeping advance and skip-stage advances.
- `AppendAuditMarkerAction` → writes the marker; releases immediately.
- `EscalateAction` → escalates the task; releases immediately.
- `CreateIsolationAction` → creates the worktree/clone artifacts
  atomically (writes `worktree_path`/`worktree_id` or
  `clone_path`/`clone_id` pair on `task_artifacts`) AND
  persists `base_commit_sha` (the merge base) in the same write
  — the live `task_artifacts` storage requires `base_commit_sha`
  whenever `worktree_path` or `clone_path` is set, otherwise it
  raises `MissingIsolationBaseError`. The merge base is computed
  from `task_artifacts.target_branch` (resolved at build time per
  §3.1) using the live isolation handlers' existing helper
  (`gobby.worktrees` / `gobby.clones`). Releases the lease
  immediately. The next heartbeat re-evaluates the candidate;
  `isolation_rule` (1.6 rule 4) now finds the artifact pair
  present and short-circuits to `None`, so `dev_rule` (1.6 rule 5)
  fires and spawns the developer into the prepared isolation.
  Persistent isolation-creation failure (e.g., disk full, git
  failure, missing target_branch) writes an audit marker and emits
  `EscalateAction` rather than retrying blindly under attended
  mode; under unattended mode, the rule's unattended-fallback
  convention from 1.6 applies.

**Acceptance:**

- 1.8.1 - Module exists. file: `src/gobby/dispatch/dispatcher.py`.
- 1.8.2 - Scanner entry point exists. symbol: `gobby.dispatch.dispatcher.run_heartbeat`.
- 1.8.3 - list_automation_candidates exists. symbol: `gobby.storage.tasks._crud.list_automation_candidates`.
- 1.8.4 - is_blocked_by_deps exists. symbol: `gobby.storage.tasks._crud.is_blocked_by_deps`.
- 1.8.5 - Candidate filter excludes claimed, leased, dep-blocked, terminal. test: `tests/dispatch/test_dispatcher.py::test_candidate_filter_excludes_claimed_leased_blocked_terminal`.
- 1.8.6 - Slot cap respected. test: `tests/dispatch/test_dispatcher.py::test_max_active_agents_cap`.
- 1.8.7 - Mutex acquire/release ordering covered. test: `tests/dispatch/test_dispatcher.py::test_mutex_lifecycle`.
- 1.8.8 - TOCTOU re-evaluation skips candidates whose tuple changed. test: `tests/dispatch/test_dispatcher.py::test_toctou_skip_on_changed_tuple`.
- 1.8.9 - First-match action executed. test: `tests/dispatch/test_dispatcher.py::test_first_match_action_executed`.
- 1.8.10 - SpawnAgentAction links run_id to lease. test: `tests/dispatch/test_dispatcher.py::test_spawn_action_links_run_id`.
- 1.8.11 - Non-spawn actions release lease immediately. test: `tests/dispatch/test_dispatcher.py::test_advance_action_releases_lease_immediately`.
- 1.8.12 - StartExpansionAction links expansion_run_id to lease. test: `tests/dispatch/test_dispatcher.py::test_start_expansion_action_links_run_id`.
- 1.8.13 - Expansion terminal events release the lease (via §1.3 handlers). test: `tests/dispatch/test_dispatcher.py::test_expansion_terminal_event_releases_lease_via_handlers`.
- 1.8.13a - Dispatcher allocates expansion_run_id and attaches it to mutex BEFORE invoking start_expansion_run_impl. test: `tests/dispatch/test_dispatcher.py::test_attach_run_id_precedes_start_expansion_run_impl`.
- 1.8.13b - Synchronous-terminal expansion still releases the lease (no race). test: `tests/dispatch/test_dispatcher.py::test_synchronous_terminal_expansion_releases_lease`.
- 1.8.13c - Terminal handler falls back to release-by-task-id when run id has no lease. test: `tests/dispatch/test_dispatcher.py::test_terminal_handler_release_by_task_id_fallback`.
- 1.8.13d - Dispatcher pins auto_apply=True on every StartExpansionAction call. test: `tests/dispatch/test_dispatcher.py::test_dispatcher_pins_auto_apply_true_on_start_expansion`.
- 1.8.14 - CreateIsolationAction writes artifact pair AND base_commit_sha atomically and releases lease. test: `tests/dispatch/test_dispatcher.py::test_create_isolation_action_writes_artifact_pair_and_base_commit_sha_atomically`.
- 1.8.14a - CreateIsolationAction resolves base_commit_sha from task_artifacts.target_branch. test: `tests/dispatch/test_dispatcher.py::test_create_isolation_action_resolves_base_commit_sha_from_target_branch`.
- 1.8.14b - CreateIsolationAction missing target_branch escalates with audit marker. test: `tests/dispatch/test_dispatcher.py::test_create_isolation_action_missing_target_branch_escalates`.
- 1.8.15 - Next heartbeat after CreateIsolationAction allows dev_rule to fire. test: `tests/dispatch/test_dispatcher.py::test_dev_rule_fires_on_next_heartbeat_after_isolation_created`.
- 1.8.16 - Startup sweep clears expired leases. test: `tests/dispatch/test_dispatcher.py::test_startup_sweep_clears_expired_leases`.

### 1.9 Dispatcher cron action handler [category: code] (depends: 1.8)

`kind: deliverable`

Target: `src/gobby/scheduler/executor.py`, `src/gobby/dispatch/dispatcher.py`

Per rev1 §1.10, expose `run_heartbeat` (1.8) so the existing cron executor
invokes it on schedule. Add a dispatcher action type (or extend the existing
executor dispatch) that routes to `gobby.dispatch.dispatcher.run_heartbeat`.
There is no `src/gobby/scheduler/handlers/` subdirectory and adding one is
out of pattern — the action handler lives next to the dispatcher itself,
and the bundled cron row pointing at this action lands in 3.5.

**Acceptance:**

- 1.9.1 - Cron executor routes the dispatcher action. behavior: "`src/gobby/scheduler/executor.py` recognizes a dispatcher action type and invokes `gobby.dispatch.dispatcher.run_heartbeat`".
- 1.9.2 - Action wiring covered. test: `tests/scheduler/test_dispatch_executor.py::test_dispatcher_action_invokes_run_heartbeat`.

### 1.10 Yolo → unattended storage core rename [category: code]

`kind: deliverable`

Target: `src/gobby/storage/migrations/<NNN>_rename_yolo_to_unattended.py`,
`src/gobby/storage/tasks/_models.py`,
`src/gobby/storage/tasks/_manager.py`,
`src/gobby/storage/tasks/_crud.py`,
`src/gobby/tasks/state_semantics.py`

Pull the storage-side yolo→unattended rename ahead of the dispatcher
helpers (1.1) so `_is_unattended` reads a real field. One-shot migration
renames `tasks.yolo` → `tasks.unattended` and backfills; the `Task` model
field follows; cascade helpers in `_manager.py` / `_crud.py` are updated to
read the new name; `state_semantics.py` rules referencing the field follow.
The build/CLI/MCP/HTTP surface (BuildOptions, profiles, CLI flags, MCP
schema, HTTP request) lands in 3.1 with explicit dep so the storage layer
is in place before any caller migrates.

**Acceptance:**

- 1.10.1 - Migration renames the column. file: `src/gobby/storage/migrations/<NNN>_rename_yolo_to_unattended.py`.
- 1.10.2 - Task model exposes the renamed field. symbol: `gobby.storage.tasks._models.Task.unattended`.
- 1.10.3 - Cascade helpers read and write the new name. behavior: "no `task.yolo` reads or `yolo = ?` SQL writes remain in `src/gobby/storage/tasks/_manager.py` or `src/gobby/storage/tasks/_crud.py` (cascade SQL UPDATE around line 375 of `_crud.py` is renamed)".
- 1.10.4 - state_semantics.py reads the new name. behavior: "no `yolo` references remain in `src/gobby/tasks/state_semantics.py`".
- 1.10.5 - Migration round-trip covered. test: `tests/storage/migrations/test_rename_yolo_to_unattended.py::test_round_trip`.
- 1.10.6 - Cascade helper covered. test: `tests/storage/tasks/test_cascade.py::test_cascade_uses_unattended_field`.

## P4 Phase 2 — Expansion, Review & Merge Agents

`kind: framing`

**Goal**: Wire expansion's manifest-driven routing to the agent layer
(rev1 §2.8/§2.8b/§2.8c), stand up the four agents the dispatcher routes to
for dev/qa/review (`developer`, `test-architect`, `qa-reviewer`,
`holistic-reviewer`), and complete the lifecycle by registering the
existing merge agents and adding the merge dispatcher rule. Sub-plan A
landed the compile path; this phase wires it to agents and review and
extends through the merge boundary.

### 2.1 Expansion agent selection [category: code]

`kind: deliverable`

Target: `src/gobby/tasks/expansion_service.py`

Per rev1 §2.8, propagate per-leaf `assigned_agent` from manifest entries
through compile output (sub-plan A landed the routing bridge; this surfaces
the field on the leaf records the dispatcher reads). Missing manifest
entries are parser errors at expansion-mode parse time per the sub-plan A
contract — there is no compile-time fallback to a default agent. If a
deterministic stub agent is needed during plan authoring, it must be
inserted into the manifest before `parse_plan(parse_mode="expansion")`,
not silently defaulted at compile.

**Acceptance:**

- 2.1.1 - Compile output carries assigned_agent on each leaf. test: `tests/tasks/test_expansion_service_compile.py::test_compile_assigns_agent_per_manifest_entry`.
- 2.1.2 - Expansion-mode parse rejects deliverables without manifest entries. test: `tests/plans/test_parser_manifest.py::test_expansion_mode_rejects_deliverable_without_manifest_entry`.
- 2.1.3 - Compile rejects missing entries (defense-in-depth). test: `tests/tasks/test_expansion_service_compile.py::test_compile_rejects_missing_manifest_entry`.

### 2.2 In-process expansion start [category: code] (depends: 2.1)

`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

Per rev1 §2.8b, the existing MCP-handler `start_expansion_run_impl`
in `src/gobby/mcp_proxy/tools/tasks/_expansion.py` is the canonical
expansion-start surface; the in-process dispatcher and the
gobby-tasks MCP closure share the same code path. There is no
`ExpansionService.start_run(...)` wrapper. This deliverable extends
the existing handler so the dispatcher can call it with a
caller-allocated `run_id` (closing the terminal-before-attach race;
see §1.8) and so the function emits the terminal events §1.3's
handlers consume.

Signature (per rev1 §2.8b, dependency-injected):

```python
def start_expansion_run_impl(
    *,
    task_manager,
    llm_service,
    config,
    completion_registry,
    triggering_session_id,
    task_id,
    plan_file=None,
    auto_apply=False,
    force_new=False,
    provider=None,
    model=None,
    project=None,
    run_id=None,  # NEW: caller-allocated run id for dispatcher attach
) -> ExpansionRunResult: ...
```

The dispatcher (§1.8) builds the call from its service container;
the MCP closure already does so. `run_id=None` (the existing path
for interactive callers) keeps the function allocating internally.
Idempotent on re-invocation against a started run.

The service emits terminal events (`expansion_run_completed`,
`expansion_run_failed`, `expansion_run_cancelled`) on every
terminal state transition so §1.3's handlers can release the
dispatch mutex keyed to `expansion_run_id` and call
`advance_lifecycle` for the right transition. Terminal events fire
even when the run reaches a terminal state synchronously inside
the call (fast validation failure or empty expansion).

**Acceptance:**

- 2.2.1 - In-process entry point lives at the MCP-handler module. symbol: `gobby.mcp_proxy.tools.tasks._expansion.start_expansion_run_impl`.
- 2.2.1a - The dispatcher and MCP closure import the same impl (no second wrapper). test: `tests/dispatch/test_dispatcher.py::test_dispatcher_imports_mcp_expansion_impl_directly`.
- 2.2.2 - Idempotent on re-call. test: `tests/mcp_proxy/tools/tasks/test_expansion.py::test_start_expansion_idempotent`.
- 2.2.3 - Used by dispatcher path with injected dependencies. test: `tests/dispatch/test_dispatcher.py::test_dispatcher_starts_expansion_with_injected_services`.
- 2.2.4 - Completion emits expansion_run_completed event. test: `tests/mcp_proxy/tools/tasks/test_expansion.py::test_completion_emits_terminal_event`.
- 2.2.5 - Failure emits expansion_run_failed event. test: `tests/mcp_proxy/tools/tasks/test_expansion.py::test_failure_emits_terminal_event`.
- 2.2.6 - Cancellation emits expansion_run_cancelled event. test: `tests/mcp_proxy/tools/tasks/test_expansion.py::test_cancellation_emits_terminal_event`.
- 2.2.7 - start_expansion_run_impl accepts caller-allocated run_id. test: `tests/mcp_proxy/tools/tasks/test_expansion.py::test_start_expansion_accepts_caller_allocated_run_id`.
- 2.2.8 - Synchronous terminal-state run still emits the terminal event. test: `tests/mcp_proxy/tools/tasks/test_expansion.py::test_synchronous_terminal_emits_event`.
- 2.2.9 - No second `ExpansionService.start_run` wrapper exists. test: `tests/tasks/test_expansion_service.py::test_no_expansion_service_start_run_wrapper`.

### 2.3 Expansion service split [category: refactor] (depends: 2.2)

`kind: deliverable`

Target: `src/gobby/tasks/expansion_service.py`,
`src/gobby/tasks/expansion/_compile.py`, `src/gobby/tasks/expansion/_apply.py`

Per rev1 §2.8c, split `expansion_service.py` along compile/apply
lines so it stays under the 1,000-line ceiling. No behavior change.
The public surface (`ExpansionService`, `compile_plan_to_spec`)
re-exports through `expansion_service.py` so existing imports keep
working.

The apply path additionally propagates `target_branch` from the
parent epic's `task_artifacts.target_branch` onto every generated
leaf's `task_artifacts.target_branch` at task-creation time. Without
this, plan-file builds (which create leaves AFTER the build-time
cascade) hit the missing-target-branch escalation path in §1.8's
`CreateIsolationAction`. Expansion is the durable inheritance point
because it is the moment new leaves are written; the build-time
cascade in §3.1 only reaches descendants that already exist.

**Acceptance:**

- 2.3.1 - Compile path lives in its own module. file: `src/gobby/tasks/expansion/_compile.py`.
- 2.3.2 - Apply path lives in its own module. file: `src/gobby/tasks/expansion/_apply.py`.
- 2.3.3 - Public API surface preserved. behavior: "tests/tasks/test_expansion_service_compile.py passes unmodified after the split".
- 2.3.4 - Module size ceiling respected. behavior: "expansion_service.py and each split module are under 1,000 lines".
- 2.3.5 - Expansion apply propagates parent's target_branch onto every generated leaf. test: `tests/tasks/expansion/test_apply.py::test_apply_copies_parent_target_branch_onto_generated_leaves`.
- 2.3.6 - Plan-file build's generated leaves reach CreateIsolationAction with target_branch set (no escalation). test: `tests/dispatch/test_dispatcher.py::test_plan_file_build_generated_leaves_have_target_branch_for_isolation`.

### 2.4 qa-reviewer contract alignment + registration [category: config]

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml` (existing)

Per rev1 §2.11, the qa-reviewer YAML already ships under the active
agents/ root. This deliverable enforces the contract via tests (read-only
permissions, structured verdict via `mark_task_review_approved` or
`mark_task_review_rejected`) and registers the agent with the
prompt-builder registry from 1.5 so the dispatcher can route to it.

**Acceptance:**

- 2.4.1 - Agent has read-only permissions (no Edit/Write). test: `tests/agents/test_qa_reviewer_definition.py::test_no_write_permissions`.
- 2.4.2 - Agent emits review verdict on a sample run. test: `tests/agents/test_qa_reviewer_definition.py::test_emits_review_verdict`.
- 2.4.3 - Agent registered with the prompt-builder registry. test: `tests/dispatch/test_prompts.py::test_qa_reviewer_prompt_builder_registered`.

### 2.5 holistic-reviewer contract alignment + registration [category: config]

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml` (existing)

Per rev1 §2.12, the holistic-reviewer YAML already ships under the active
agents/ root. This deliverable enforces the three-outcome contract
(approve / reject / escalate) and subtree-read permissions via tests, and
registers the agent with the prompt-builder registry.

**Acceptance:**

- 2.5.1 - Three-outcome contract enforced. test: `tests/agents/test_holistic_reviewer_definition.py::test_three_outcomes`.
- 2.5.2 - Reads epic subtree via task tools. test: `tests/agents/test_holistic_reviewer_definition.py::test_reads_subtree`.
- 2.5.3 - Agent registered with the prompt-builder registry. test: `tests/dispatch/test_prompts.py::test_holistic_reviewer_prompt_builder_registered`.

### 2.6 expansion-qa harness [category: code] (depends: 2.3)

`kind: deliverable`

Target: `src/gobby/tasks/expansion/_qa.py`

Per rev1 §2.13, multi-mode verification harness for an expansion run: shape
check, manifest coverage check, routing check. The dispatcher calls this
between compile and apply so a misrouted run is caught before agents spawn.
New module.

**Acceptance:**

- 2.6.1 - Module exists. file: `src/gobby/tasks/expansion/_qa.py`.
- 2.6.2 - Coverage check exists. symbol: `gobby.tasks.expansion._qa.check_manifest_coverage`.
- 2.6.3 - Routing check exists. symbol: `gobby.tasks.expansion._qa.check_routing`.
- 2.6.4 - Harness covered. test: `tests/tasks/expansion/test_qa.py::test_check_manifest_coverage_rejects_missing_leaves`.
- 2.6.5 - Routing-check covered. test: `tests/tasks/expansion/test_qa.py::test_check_routing_rejects_unknown_agent`.

### 2.7 test-architect contract alignment + registration [category: config]

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/test-architect.yaml` (existing)

Per rev1 §1.6a/§1.7, the test-architect YAML already ships under the
active agents/ root. The user-invocable `/gobby test-arch` skill defers
post-0.4.0; this deliverable enforces the agent contract and registers
the agent with the prompt-builder registry from 1.5 so dispatcher routing
has a target.

**Acceptance:**

- 2.7.1 - Agent definition loads. test: `tests/agents/test_test_architect_definition.py::test_definition_loads`.
- 2.7.2 - Agent registered with the prompt-builder registry. test: `tests/dispatch/test_prompts.py::test_test_architect_prompt_builder_registered`.

### 2.8 developer agent (active root) [category: config]

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/developer.yaml`

Per rev1 §1.6a/§1.7, ship the autonomous developer agent at the active
install root. The previous developer agent was retired and lives at
`src/gobby/install/shared/workflows/agents/deprecated/developer.yaml` per
the §2.14b deprecation pattern; that tombstone stays in place and bundled
sync soft-deletes the deprecated row. The new YAML at the active root
replaces it for autonomous routing.

**Acceptance:**

- 2.8.1 - Agent YAML exists at the active root. file: `src/gobby/install/shared/workflows/agents/developer.yaml`.
- 2.8.2 - Deprecated tombstone left in place. file: `src/gobby/install/shared/workflows/agents/deprecated/developer.yaml`.
- 2.8.3 - Agent registered with the prompt-builder registry. test: `tests/dispatch/test_prompts.py::test_developer_prompt_builder_registered`.

### 2.9 Merge agents contract alignment + registration [category: config] (depends: 1.7)

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml` (existing),
`src/gobby/install/shared/workflows/agents/merge-worker.yaml` (existing),
`src/gobby/mcp_proxy/tools/tasks/_lifecycle.py` (or current home of
`mark_task_review_approved` MCP registration)

Per rev1 §1.7, the merge agents already ship under the active agents/
root. This deliverable:

1. Registers both agents with the prompt-builder registry from 1.5 so
   the dispatcher rule (2.10) can route to them.
2. Exposes `mark_task_merged(task_id, pr_url=None, merge_sha=None)`,
   `mark_task_merge_failed(task_id, reason)`, and
   `mark_task_pr_opened(task_id, pr_url)` from §1.7 as gobby-tasks
   MCP tools, mirroring the existing `mark_task_review_approved` /
   `mark_task_review_rejected` tools (same module, same registration
   pattern, same schema-validation surface). `mark_task_pr_opened` is
   the operator-facing writer that lifts the attended-pr_rule
   escalation back into the dispatcher's matrix.
3. Adds the two new MCP tools to the merge-orchestrator and
   merge-worker permissions allowlist in their YAML definitions so the
   YAML agents (which cannot call Python helpers directly) can perform
   terminal lifecycle write-back via the constrained tool surface.
4. Enforces the merge dispatcher contract via tests — lifecycle
   write-back of a `pr_url` artifact and advance to the merged stage
   via the new MCP tool; conflict/retry honoring
   `BuildConfig.max_merge_attempts`; unattended-mode fallback when
   conflicts cannot be resolved cleanly.

The contract closes the loop the §2.10 merge_rule and §1.6 documentation
both reference: the dispatcher emits `SpawnAgentAction(merge-orchestrator)`,
the agent does its work, the agent calls
`gobby-tasks:mark_task_merged(...)` to set `(merged, closed)` with
`pr_url` and trigger cascade-close — every step happens through an
allowed tool that the agent's YAML actually exposes.

**Acceptance:**

- 2.9.1 - merge-orchestrator registered with the prompt-builder registry. test: `tests/dispatch/test_prompts.py::test_merge_orchestrator_prompt_builder_registered`.
- 2.9.2 - merge-worker registered with the prompt-builder registry. test: `tests/dispatch/test_prompts.py::test_merge_worker_prompt_builder_registered`.
- 2.9.3 - mark_task_merged registered as a gobby-tasks MCP tool with required task_id and optional pr_url/merge_sha. behavior: "`list_tools(server_name='gobby-tasks')` returns `mark_task_merged` with parameters `task_id` (required), `pr_url` (optional), `merge_sha` (optional)".
- 2.9.4 - mark_task_merge_failed registered as a gobby-tasks MCP tool. behavior: "`list_tools(server_name='gobby-tasks')` returns `mark_task_merge_failed` with parameters `task_id`, `reason`".
- 2.9.4a - mark_task_pr_opened registered as a gobby-tasks MCP tool. behavior: "`list_tools(server_name='gobby-tasks')` returns `mark_task_pr_opened` with parameters `task_id` (required) and `pr_url` (required)".
- 2.9.4b - mark_task_pr_opened tool advances (pr, open) → (pr, needs_review). test: `tests/mcp_proxy/tools/tasks/test_lifecycle_tools.py::test_mark_task_pr_opened_tool_from_open`.
- 2.9.4c - mark_task_pr_opened tool recovers (pr, escalated) → (pr, needs_review) and clears escalation metadata. test: `tests/mcp_proxy/tools/tasks/test_lifecycle_tools.py::test_mark_task_pr_opened_tool_recovers_from_attended_pr_creation_escalation`.
- 2.9.5 - mark_task_merged tool sets (merged, closed); covers worktree-merge, clone-merge, and skipped-PR/unattended paths. test: `tests/mcp_proxy/tools/tasks/test_lifecycle_tools.py::test_mark_task_merged_tool_covers_worktree_clone_and_skipped_pr_paths`.
- 2.9.6 - mark_task_merge_failed tool routes per attended/unattended. test: `tests/mcp_proxy/tools/tasks/test_lifecycle_tools.py::test_mark_task_merge_failed_tool_routes_per_attended_mode`.
- 2.9.7 - merge-orchestrator allowlist includes the two new MCP tools. test: `tests/agents/test_merge_lifecycle.py::test_merge_orchestrator_allowlist_includes_merge_tools`.
- 2.9.8 - merge-worker allowlist includes the two new MCP tools. test: `tests/agents/test_merge_lifecycle.py::test_merge_worker_allowlist_includes_merge_tools`.
- 2.9.9 - Merge lifecycle write-back covered end-to-end through the tool. test: `tests/agents/test_merge_lifecycle.py::test_pr_url_artifact_set_on_merge_via_tool`.
- 2.9.10 - Conflict/retry policy enforced. test: `tests/agents/test_merge_lifecycle.py::test_conflict_retry_within_max_merge_attempts`.
- 2.9.11 - Unattended fallback on unresolved conflict. test: `tests/agents/test_merge_lifecycle.py::test_unattended_fallback_on_unresolved_conflict`.

### 2.10 Merge dispatcher rule [category: code] (depends: 1.6, 2.9)

`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`

§2.10 owns `merge_rule` end-to-end — implementation, registration,
and all tests. §1.6 ships `BASE_RULES` (the ten non-merge rules in
order); this deliverable defines `merge_rule` and exports the final
`RULES = [*BASE_RULES, merge_rule]` from
`src/gobby/dispatch/rules.py`, which the dispatcher (1.8) imports.
The split keeps both ownership invariants durably testable: §1.6
asserts `BASE_RULES` has exactly ten entries excluding `merge_rule`;
§2.10 asserts the final `RULES` is `BASE_RULES + [merge_rule]` with
eleven entries and `merge_rule` at position 11. No other deliverable
writes to `gobby.dispatch.rules.merge_rule`.

The rule routes tasks at the existing `Lifecycle.merging` enum value
(the gerund covers both queued and in-flight merge work) on epics to
the merge-orchestrator agent (2.9) via a `SpawnAgentAction`. The
dispatcher rule does NOT advance lifecycle — the merge agent owns
terminal write-back by calling `gobby-tasks:mark_task_merged(...)` or
`gobby-tasks:mark_task_merge_failed(...)` (the agent-facing MCP tools
registered in §2.9, which dispatch through `advance_lifecycle` from
§1.7) after performing its own conflict-resolution and cleanup. The
rule honors `unattended` per the unattended-fallback convention from
§1.6 and respects `BuildConfig.max_merge_attempts`; on exhaust under
attended mode, the rule emits an `EscalateAction` with reason
`merge_failed:max_attempts`.

**Acceptance:**

- 2.10.1 - Merge rule symbol exists in dispatch.rules. symbol: `gobby.dispatch.rules.merge_rule`.
- 2.10.2 - Final RULES export equals BASE_RULES + [merge_rule] with merge_rule at position 11. test: `tests/dispatch/test_rules.py::test_final_rules_is_base_rules_plus_merge_rule_at_position_11`.
- 2.10.3 - Routes (merging, open) on epics to merge-orchestrator. test: `tests/dispatch/test_rules.py::test_merge_rule_routes_on_merging_stage`.
- 2.10.4 - Rule does not emit AdvanceLifecycleAction. test: `tests/dispatch/test_rules.py::test_merge_rule_does_not_advance_lifecycle`.
- 2.10.5 - Respects max_merge_attempts. test: `tests/dispatch/test_rules.py::test_merge_rule_caps_retries`.
- 2.10.6 - Attended exhaust escalates with reason. test: `tests/dispatch/test_rules.py::test_merge_rule_escalates_on_max_attempts`.
- 2.10.7 - Unattended exhaust falls back per 1.6's convention. test: `tests/dispatch/test_rules.py::test_merge_rule_unattended_fallback_on_max_attempts`.

## P5 Phase 3 — Build Service & CLI

`kind: framing`

**Goal**: Reconcile the per-task automation surface (rename `yolo` →
`unattended` across CLI/MCP/HTTP/profiles, add stubbed `--yolo` for
composer), introduce the bundled system cron row for the dispatcher and
the project-wide halt escape hatch (`gobby build stop` toggles the cron
row), and land the rev1 housekeeping items (§2.14a/b, §2.16–§2.20) plus
the configurability convention.

### 3.1 Yolo → unattended build/CLI/MCP/HTTP surface [category: code] (depends: 1.10)

`kind: deliverable`

Target: `src/gobby/build/service.py`,
`src/gobby/cli/build.py`,
`src/gobby/config/build.py`,
`src/gobby/mcp_proxy/tools/build.py`,
`src/gobby/servers/routes/build.py`

Carry the storage rename (1.10) through the user-facing surface, with
a careful split between the deprecated `unattended` alias and the new
composer-stub field on JSON-shaped surfaces (MCP/HTTP) where field
names cannot collide. Also preserve the existing rev1
`BuildOptions.target_branch` contract across the rename: the build
service resolves `target_branch=None` to `git rev-parse --abbrev-ref
HEAD` for plan-file or epic builds, validates explicit values against
`git rev-parse --verify`, and persists the resolved branch on
`task_artifacts.target_branch` BEFORE any `CreateIsolationAction`
emits — `CreateIsolationAction` from §1.4 / §1.8 reads
`task_artifacts.target_branch` to compute and persist
`base_commit_sha`. Leaf builds inherit `target_branch` from their
parent epic via the existing build cascade and do not set
`task_artifacts.target_branch` directly on the leaf.

Surfaces touched:

- `BuildOptions.yolo` → `unattended`. Add new
  `BuildOptions.composer_yolo: bool` for the composer-stub flag from
  3.2.
- `BuildConfig` profile names `default_yolo` → `default_unattended` and
  `full-yolo` → `full-unattended`, with backward-compat aliases (legacy
  names continue to resolve to the new profiles for one minor release).
- MCP `gobby-tasks-ops:build_task` schema: rename `yolo` → `unattended`;
  legacy `yolo` accepted as deprecated alias with a warning for one
  minor release; new `composer_yolo` field added separately.
- HTTP `BuildRequest`: rename `yolo` → `unattended`; add
  `composer_yolo`.
- The `--yolo` CLI flag (3.2) maps to `BuildOptions.composer_yolo=True`,
  NOT to `unattended`. The legacy CLI flag name `--yolo` is reserved
  for composer-stub semantics; the deprecated-`yolo` alias only applies
  to the JSON field name on MCP/HTTP, not to the CLI flag.

**Acceptance:**

- 3.1.1 - BuildOptions carries unattended. symbol: `gobby.build.service.BuildOptions.unattended`.
- 3.1.2 - BuildOptions carries composer_yolo. symbol: `gobby.build.service.BuildOptions.composer_yolo`.
- 3.1.3 - BuildConfig profiles renamed with aliases. behavior: "`src/gobby/config/build.py` declares `default_unattended` and `full-unattended` profiles; legacy names `default_yolo` and `full-yolo` resolve to them via alias for one minor release".
- 3.1.4 - MCP build_task schema accepts unattended (yolo as deprecated alias). behavior: "`gobby-tasks-ops:build_task` schema accepts `unattended`; passing `yolo` emits a deprecation warning and maps to `unattended` for one minor release".
- 3.1.5 - MCP build_task schema accepts composer_yolo as a separate field. behavior: "`gobby-tasks-ops:build_task` schema declares `composer_yolo: bool` distinct from `unattended` and the deprecated `yolo` alias".
- 3.1.6 - HTTP BuildRequest carries unattended and composer_yolo. symbol: `gobby.servers.routes.build.BuildRequest.unattended`.
- 3.1.7 - Profile-alias resolution covered. test: `tests/config/test_build_profiles.py::test_legacy_yolo_profile_aliases_resolve`.
- 3.1.8 - MCP schema rename + composer_yolo addition covered. test: `tests/mcp_proxy/tools/test_build.py::test_unattended_with_composer_yolo_distinct_fields`.
- 3.1.9 - HTTP route covers both fields. test: `tests/servers/routes/test_build.py::test_buildrequest_unattended_and_composer_yolo`.
- 3.1.10 - BuildOptions.target_branch preserved across rename. symbol: `gobby.build.service.BuildOptions.target_branch`.
- 3.1.11 - target_branch=None resolves to current branch on plan-file/epic builds. test: `tests/build/test_target_branch.py::test_target_branch_none_resolves_to_head_on_plan_or_epic_build`.
- 3.1.12 - Explicit target_branch is validated against git rev-parse --verify. test: `tests/build/test_target_branch.py::test_explicit_target_branch_validated`.
- 3.1.13 - task_artifacts.target_branch persisted before CreateIsolationAction emits. test: `tests/build/test_target_branch.py::test_target_branch_persisted_before_isolation_action`.
- 3.1.14 - Leaf builds inherit target_branch via cascade and do not write it directly. test: `tests/build/test_target_branch.py::test_leaf_build_inherits_target_branch_via_cascade`.

### 3.2 CLI flags: --unattended and --yolo [category: code] (depends: 3.1)

`kind: deliverable`

Target: `src/gobby/cli/build.py`

Wire the renamed `--unattended` flag (maps to
`BuildOptions.unattended`) and the new `--yolo` flag (maps to
`BuildOptions.composer_yolo`, composer-stub: accepted but no-ops at
runtime). Both flags flow through the shared build service. On JSON
surfaces (MCP/HTTP), the two semantics map to the distinct fields
defined in 3.1 (`unattended` and `composer_yolo`) so they cannot
collide.

**Acceptance:**

- 3.2.1 - --unattended flag exists and maps to BuildOptions.unattended. symbol: `gobby.cli.build.build_command`.
- 3.2.2 - --yolo flag exists and maps to BuildOptions.composer_yolo. behavior: "click.option(`--yolo`) on `gobby.cli.build.build_command` sets `BuildOptions.composer_yolo`, not `BuildOptions.unattended`".
- 3.2.3 - --yolo no-ops at runtime (composer stub). test: `tests/build/test_yolo_unattended_split.py::test_yolo_flag_is_noop_with_composer_stub`.
- 3.2.4 - Flags propagate through MCP and HTTP as distinct JSON fields. test: `tests/build/test_yolo_unattended_split.py::test_flag_propagation_distinct_fields_on_json_surfaces`.
- 3.2.5 - --target-branch flag exists and maps to BuildOptions.target_branch. test: `tests/build/test_target_branch.py::test_target_branch_flag_maps_to_build_options`.

### 3.3 Composer-scope cap [category: code] (depends: 3.2)

`kind: deliverable`

Target: `src/gobby/build/service.py`

Defense-in-depth: build-time validation in `gobby.build.service.build` that
caps composer authority by entry-point type. Plan-file or epic entry caps
composer at plan; leaf entry skips composer entirely; ideate-prompt or
empty entry gives upstream authority. Validation runs even though the
composer is stubbed, so the contract holds when the composer lands.

**Acceptance:**

- 3.3.1 - Cap helper exists. symbol: `gobby.build.service._composer_scope_cap`.
- 3.3.2 - Plan/epic/leaf entry rejects upstream composer authority. test: `tests/build/test_yolo_unattended_split.py::test_composer_cap_rejects_upstream_for_plan_epic_leaf`.
- 3.3.3 - Ideate/empty entry allows upstream authority. test: `tests/build/test_yolo_unattended_split.py::test_composer_cap_allows_upstream_for_ideate`.

### 3.4 Cron is_system column migration [category: code]

`kind: deliverable`

Target: `src/gobby/storage/migrations/<NNN>_add_cron_is_system.py`,
`src/gobby/storage/cron_models.py`,
`src/gobby/storage/cron.py`

Add `is_system: bool` column to the `cron_jobs` table (default `false`).
Mirrors the convention behind system projects (name-reservation in
`SYSTEM_PROJECT_NAMES`) and system sessions (fixed UUID
`SYSTEM_SESSION_ID`); cron rows can grow over time as gobby ships more
bundled jobs (health checks, expiration sweeps, etc.) so a column scales
better than a frozenset of names. The protection helpers that enforce
`is_system` semantics live in 3.6.

**Acceptance:**

- 3.4.1 - Migration adds the column. file: `src/gobby/storage/migrations/<NNN>_add_cron_is_system.py`.
- 3.4.2 - CronJob model exposes is_system. symbol: `gobby.storage.cron_models.CronJob.is_system`.
- 3.4.3 - Migration round-trip covered. test: `tests/storage/migrations/test_add_cron_is_system.py::test_round_trip`.
- 3.4.4 - list_cron_jobs supports filtering by is_system. test: `tests/storage/test_cron.py::test_list_jobs_filters_by_is_system`.

### 3.5 Bundled dispatcher cron row install [category: code] (depends: 3.4, 1.9)

`kind: deliverable`

Target: `src/gobby/runner.py`, `src/gobby/storage/cron.py`

Insert the dispatcher cron row at daemon startup with
`is_system=true`, action invoking the wiring from 1.9. The startup
flow distinguishes first-install from upgrade-reconciliation so
operator-set schedule overrides survive across daemon restarts:

- **First install (row does not exist):** insert a fresh row with
  the bundled action (`action_type` / `action_config` from §1.9)
  AND the bundled schedule defaults (e.g., every 30 seconds);
  `is_system=true`; `enabled=true` by default.
- **Upgrade reconciliation (row already exists):** call
  `reconcile_system_job_definition` from §3.6 to repair ONLY the
  bundled action fields (`action_type` and `action_config`).
  Schedule fields (`schedule_type`, `cron_expr`, `interval_seconds`,
  `run_at`, `timezone`) are operator-owned once the row exists and
  are NOT touched on reconciliation. The `enabled` value is
  preserved (so user-disabled state survives upgrades).

This split honors both contracts: operators retain durable control
of `enabled` and the schedule fields via the public `update_job`
surface (§3.6 allowlist); §3.6 still owns the project-wide halt
invariant for `action_type` / `action_config` (only the internal
reconciler can rewrite them).

**Acceptance:**

- 3.5.1 - Bundled install at startup creates or reconciles the row. symbol: `gobby.runner.install_dispatcher_cron_row`.
- 3.5.2 - Row is_system=true after install. test: `tests/scheduler/test_dispatch_registration.py::test_dispatcher_row_marked_system`.
- 3.5.3 - Existing enabled value preserved across reconciliation. test: `tests/scheduler/test_dispatch_registration.py::test_existing_enabled_preserved_on_upgrade`.
- 3.5.4 - First install seeds bundled action AND schedule defaults. test: `tests/scheduler/test_dispatch_registration.py::test_first_install_seeds_action_and_schedule_defaults`.
- 3.5.5 - Upgrade reconciliation repairs action_type and action_config only. test: `tests/scheduler/test_dispatch_registration.py::test_upgrade_reconciles_action_only`.
- 3.5.6 - Operator-updated schedule survives startup reconciliation. test: `tests/scheduler/test_dispatch_registration.py::test_operator_set_schedule_survives_upgrade`.
- 3.5.7 - Drifted action_config is repaired even if operator changed schedule. test: `tests/scheduler/test_dispatch_registration.py::test_action_config_repaired_with_operator_schedule_intact`.

### 3.6 System cron row protection helpers [category: code] (depends: 3.4)

`kind: deliverable`

Target: `src/gobby/storage/cron.py`

Add protection helpers that refuse identity-or-action-mutating ops
on the OPERATOR-FACING surface of rows where `is_system=true`,
using an explicit allowlist. The storage class is `CronJobStorage`
in `src/gobby/storage/cron.py`; existing methods are `delete_job`,
`update_job`, `toggle_job` (the `*_cron_job` names are the
gobby-cron MCP tool surface, which call through to these storage
methods).

**Two surfaces, distinct protections:**

1. **Operator-facing surface** — public `update_job`, `delete_job`,
   `toggle_job`, and any rename op. These are reachable via CLI,
   MCP, and HTTP. For `is_system=true` rows, `update_job` accepts
   ONLY these fields:

   - `enabled` (toggle dispatcher on/off; the project-wide halt path)
   - `schedule_type`
   - `cron_expr`
   - `interval_seconds`
   - `run_at`
   - `timezone`

   Any attempt to update `name`, `action_type`, `action_config`,
   `next_run_at`, `last_run_at`, `last_status`,
   `consecutive_failures`, or any other identity / action /
   bookkeeping field on a system row via this surface raises
   `SystemRowProtected`. `delete_job` and any rename op also
   raise. `toggle_job` is allowed (it routes through the
   bookkeeping path internally to recompute `next_run_at`, which
   the operator surface itself does not have to allow). The
   allowlist is defined as a single constant
   (`SYSTEM_ROW_UPDATE_ALLOWED_FIELDS`) in
   `src/gobby/storage/cron.py` so future schedule-field additions
   land in one place.

2. **System-owned internal writers** — two new caller-scoped
   methods on `CronJobStorage` reachable only from the scheduler
   and the bootstrap reconciler, NOT from MCP / CLI / HTTP:

   - `update_system_job_bookkeeping(job_id, *, next_run_at=UNSET,
     last_run_at=UNSET, last_status=UNSET,
     consecutive_failures=UNSET)` — writes the scheduler's own
     state. Used by `CronScheduler._check_due_jobs` (advances
     `next_run_at` after dispatch) and
     `CronScheduler._execute_and_update` (writes execution
     telemetry). Uses an `UNSET` sentinel (defined as a private
     module-level singleton in `src/gobby/storage/cron.py`) so
     omitted fields are PRESERVED on the existing row and explicit
     `None` writes are POSSIBLE — required because the scheduler
     needs to clear `next_run_at = NULL` on disabled or one-shot
     paths without clobbering `last_run_at` / `last_status` /
     `consecutive_failures`, and conversely `_execute_and_update`
     writes telemetry without touching `next_run_at`. Implementation
     filters `UNSET` from the SQL UPDATE column list; `None`
     becomes a real `NULL` write. Refuses every non-bookkeeping
     field; raises `SystemRowProtected` if called against a
     non-system row (it's defined exclusively for the system
     surface).
   - `reconcile_system_job_definition(job_id, *, action_type,
     action_config)` — startup-only repair, scoped to bundled
     ACTION fields only. Called from
     `gobby.runner.install_dispatcher_cron_row` (3.5) to repair
     `action_type` / `action_config` if they drift from bundled
     values across upgrades. Schedule fields are operator-owned
     once the row exists (the operator can update them via the
     public `update_job` surface; the reconciler does not touch
     them — see §3.5 for the first-install vs. upgrade split).
     Preserves the operator-set `enabled` value (3.5.3). Raises
     `SystemRowProtected` if called against a non-system row,
     and exits early without writes if the action fields are
     already in sync.

   These methods are NOT registered on the gobby-cron MCP server,
   so external callers cannot invoke them. The scheduler and
   bootstrap reconciler import them directly.

This closes the protection gap completely: the dispatcher cron
row's action and identity cannot be rewritten by operators or
agents (preserving the project-wide halt invariant), but
the scheduler's own bookkeeping and bundled startup reconciliation
both have explicit, scoped paths that work.

Error messages on `SystemRowProtected` identify the row as
gobby-managed, name the specific field that was rejected, and
reference the appropriate internal writer if applicable.

**Acceptance:**

- 3.6.1 - delete_job refuses system rows. test: `tests/storage/test_cron.py::test_delete_refuses_system_row`.
- 3.6.2 - update_job(enabled=...) allowed on system rows. test: `tests/storage/test_cron.py::test_enabled_update_allowed_on_system_row`.
- 3.6.3 - update_job with each schedule field (schedule_type / cron_expr / interval_seconds / run_at / timezone) allowed on system rows. test: `tests/storage/test_cron.py::test_schedule_field_updates_allowed_on_system_row`.
- 3.6.4 - update_job(name=...) refused on system rows. test: `tests/storage/test_cron.py::test_name_update_refused_on_system_row`.
- 3.6.5 - update_job(action_type=...) refused on system rows. test: `tests/storage/test_cron.py::test_action_type_update_refused_on_system_row`.
- 3.6.6 - update_job(action_config=...) refused on system rows. test: `tests/storage/test_cron.py::test_action_config_update_refused_on_system_row`.
- 3.6.6a - update_job(next_run_at=...) and other bookkeeping fields refused via the operator surface on system rows. test: `tests/storage/test_cron.py::test_bookkeeping_fields_refused_via_operator_surface`.
- 3.6.7 - SYSTEM_ROW_UPDATE_ALLOWED_FIELDS constant defines the allowlist. symbol: `gobby.storage.cron.SYSTEM_ROW_UPDATE_ALLOWED_FIELDS`.
- 3.6.8 - Error messages identify rows as system-managed and name the rejected field. test: `tests/storage/test_cron.py::test_protection_error_message_names_system_and_field`.
- 3.6.9 - update_system_job_bookkeeping writes scheduler state on system rows. symbol: `gobby.storage.cron.CronJobStorage.update_system_job_bookkeeping`.
- 3.6.10 - update_system_job_bookkeeping refuses non-system rows. test: `tests/storage/test_cron.py::test_bookkeeping_refuses_non_system_row`.
- 3.6.11 - update_system_job_bookkeeping rejects non-bookkeeping fields. test: `tests/storage/test_cron.py::test_bookkeeping_rejects_non_bookkeeping_fields`.
- 3.6.11a - Updating only next_run_at preserves last_run_at, last_status, and consecutive_failures. test: `tests/storage/test_cron.py::test_bookkeeping_partial_update_preserves_telemetry`.
- 3.6.11b - Updating only execution telemetry preserves next_run_at. test: `tests/storage/test_cron.py::test_bookkeeping_telemetry_update_preserves_next_run_at`.
- 3.6.11c - Explicit None writes NULL to the named field without clearing others. test: `tests/storage/test_cron.py::test_bookkeeping_explicit_none_writes_null_without_clobbering_others`.
- 3.6.11d - UNSET sentinel exists. symbol: `gobby.storage.cron.UNSET`.
- 3.6.12 - reconcile_system_job_definition repairs action_type and action_config on system rows. symbol: `gobby.storage.cron.CronJobStorage.reconcile_system_job_definition`.
- 3.6.13 - reconcile_system_job_definition refuses non-system rows. test: `tests/storage/test_cron.py::test_reconcile_refuses_non_system_row`.
- 3.6.14 - reconcile_system_job_definition is no-op when action fields already in sync. test: `tests/storage/test_cron.py::test_reconcile_no_op_when_action_in_sync`.
- 3.6.14a - reconcile_system_job_definition does NOT touch schedule fields. test: `tests/storage/test_cron.py::test_reconcile_does_not_overwrite_schedule_fields`.
- 3.6.15 - update_system_job_bookkeeping / reconcile_system_job_definition NOT registered on gobby-cron MCP. test: `tests/mcp_proxy/tools/test_cron_tools.py::test_internal_writers_not_exposed_via_mcp`.
- 3.6.16 - CronScheduler advances next_run_at on the dispatcher row via the bookkeeping writer. test: `tests/scheduler/test_cron_scheduler.py::test_scheduler_advances_dispatcher_next_run_at`.
- 3.6.17 - toggle_job on system rows recomputes next_run_at via the bookkeeping path. test: `tests/storage/test_cron.py::test_toggle_job_on_system_row_recomputes_next_run_at`.

### 3.7 gobby build stop / resume via cron toggle [category: code] (depends: 3.5, 3.6)

`kind: deliverable`

Target: `src/gobby/build/service.py`, `src/gobby/cli/build.py`

Add `build_stop` and `build_resume` entry points to the shared build
service. `stop` calls `CronJobStorage.update_job(enabled=False)` on the
bundled dispatcher cron row (3.5); in-flight agents finish their current
task naturally and no further dispatch fires until resume. `resume`
calls `update_job(enabled=True)`. A project-level lifecycle event
records the halt/resume decision for audit.

`_kick_dispatcher_tick()` (currently at `src/gobby/build/service.py:357`,
called from three sites in the same file) must check the bundled
dispatcher cron row's `enabled` flag before firing; if disabled, the
tick no-ops with a structured log line and the build state-write
completes normally. This shared guard is the single source of truth —
both the cron heartbeat (1.9) and the post-build kick honor the same
cron-row `enabled` value, so `gobby build stop` followed by another
`gobby build` cannot bypass the halt.

No `--task <ref>` flag in this cut — subtree pause is post-0.4.0 (the
ledger pattern that would make per-task flip-and-restore safe is
deferred); explicit per-task opt-out remains available via
`gobby build` itself.

**Acceptance:**

- 3.7.1 - Stop entry point exists. symbol: `gobby.build.service.build_stop`.
- 3.7.2 - Resume entry point exists. symbol: `gobby.build.service.build_resume`.
- 3.7.3 - CLI subcommand wired. symbol: `gobby.cli.build.build_stop_command`.
- 3.7.4 - Stop disables the dispatcher cron row. test: `tests/build/test_build_stop.py::test_stop_disables_dispatcher_cron`.
- 3.7.5 - Resume re-enables the dispatcher cron row. test: `tests/build/test_build_stop.py::test_resume_enables_dispatcher_cron`.
- 3.7.6 - Stop writes a project-level lifecycle event. test: `tests/build/test_build_stop.py::test_lifecycle_event_appended`.
- 3.7.7 - In-flight agents are not killed by stop. test: `tests/build/test_build_stop.py::test_in_flight_agents_unaffected`.
- 3.7.8 - _kick_dispatcher_tick respects cron row enabled flag. test: `tests/build/test_build_stop.py::test_kick_no_op_when_dispatcher_disabled`.
- 3.7.9 - kick fires when dispatcher is enabled. test: `tests/build/test_build_stop.py::test_kick_fires_when_dispatcher_enabled`.
- 3.7.10 - No --task flag is exposed. test: `tests/build/test_build_stop.py::test_no_task_flag_exposed`.

### 3.8 Hook/rule scoping for build agents [category: config]

`kind: deliverable`

Target: `src/gobby/install/shared/rules/build/`

Per rev1 §2.14a, scope autonomous build-agent rules by `audience:
autonomous` so interactive sessions are not blocked by them. Per §2.14b,
the deprecated rules they replace move to a tombstoned `deprecated/`
subdirectory and are excluded from active sync.

**Acceptance:**

- 3.8.1 - Audience-scoped build rules exist. behavior: "every YAML under `src/gobby/install/shared/rules/build/` declares `audience: autonomous`".
- 3.8.2 - Deprecated rules tombstoned. file: `src/gobby/install/shared/rules/build/deprecated/`.
- 3.8.3 - Audience scoping covered. test: `tests/workflows/test_rule_audience_scoping.py::test_build_rules_autonomous_only`.
- 3.8.4 - Deprecated rules excluded from active sync. test: `tests/workflows/test_rule_loader.py::test_deprecated_rules_not_synced`.

### 3.9 Document grandfather/legacy retirement [category: docs]

`kind: deliverable`

Target: `docs/contracts/plan-coverage.md`,
`tests/install/test_no_grandfather_files.py` (new)

Per rev1 §2.16, the grandfathered classification escape hatch is fully
retired by sub-plan A's manifest contract. A pre-flight scan confirms no
`.grandfathered*` or `.legacy-classification.yaml` files exist anywhere
under `src/gobby/install/shared/` (the typed plan-coverage contract is
already the single routing source). This deliverable documents the
retirement and adds a guardrail test that fails loud if the files are
ever re-introduced.

**Acceptance:**

- 3.9.1 - Plan-coverage doc records the retirement. behavior: "`docs/contracts/plan-coverage.md` states that grandfather/legacy classification escape hatches are retired by the typed contract".
- 3.9.2 - Guardrail test fails on re-introduction. test: `tests/install/test_no_grandfather_files.py::test_no_grandfather_files_under_install`.

### 3.10 Coverage manifest lifecycle [category: code]

`kind: deliverable`

Target: `src/gobby/storage/plans.py`

Per rev1 §2.17, system-managed lifecycle for the coverage manifest:
generated on plan create, regenerated on `plan_hash` bump, removed on
archive. Wired through `LocalPlanManager`.

**Acceptance:**

- 3.10.1 - Generation hook exists. symbol: `gobby.storage.plans.LocalPlanManager._generate_coverage_manifest`.
- 3.10.2 - Hash-bump regeneration covered. test: `tests/storage/test_plans.py::test_update_plan_hash_regenerates_manifest`.
- 3.10.3 - Archive removes manifest. test: `tests/storage/test_plans.py::test_archive_removes_coverage_manifest`.

### 3.11 Auto-move plans on epic terminal [category: code]

`kind: deliverable`

Target: `src/gobby/hooks/event_handlers/_plan.py`

Per rev1 §2.18, when an epic linked to a plan reaches a terminal lifecycle
state (closed-completed or closed-obsolete), move
`.gobby/plans/<plan>.md` to `.gobby/plans/completed/`. The plan registry
flips `state: archived` and the coverage manifest is removed (3.10). New
module.

**Acceptance:**

- 3.11.1 - Plan-move handler exists. symbol: `gobby.hooks.event_handlers._plan.on_epic_terminal`.
- 3.11.2 - Closed-completed moves to completed/. test: `tests/hooks/event_handlers/test_plan.py::test_completed_plan_archived`.
- 3.11.3 - Plan registry flips state. test: `tests/hooks/event_handlers/test_plan.py::test_plan_state_archived`.

### 3.12 CLI overrides for BuildConfig retry caps [category: code]

`kind: deliverable`

Target: `src/gobby/cli/build.py`, `src/gobby/build/service.py`

Per rev1 §2.19, `BuildConfig` at `src/gobby/config/build.py` already
exposes per-stage retry caps as individual fields
(`max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`,
`max_holistic_rounds`, `max_review_rounds`). The CLI does not yet expose
them as overrideable flags. Add per-cap CLI flags that flow through
`BuildOptions` into `BuildConfig` and reach the dispatcher's action
executor.

**Acceptance:**

- 3.12.1 - BuildConfig per-stage caps already exist. symbol: `gobby.config.build.BuildConfig.max_expansion_attempts`.
- 3.12.2 - Per-stage cap flags exist on build command. behavior: "click options `--max-expansion-attempts`, `--max-qa-rounds`, `--max-merge-attempts`, `--max-holistic-rounds`, `--max-review-rounds` declared on `gobby.cli.build.build_command`".
- 3.12.3 - CLI overrides reach dispatcher. test: `tests/build/test_retry_caps.py::test_cli_overrides_propagate_to_dispatcher`.

### 3.13 Configurability convention [category: docs]

`kind: deliverable`

Target: `src/gobby/install/shared/CLAUDE.md`,
`src/gobby/workflows/loader.py`

Pin the gobby-tagged-immutable + user-cloneable convention: bundled
templates ship with `source: gobby` and refuse mutation; users override by
copying to project-local `.gobby/install/<kind>/<name>/` paths. Add
fail-loud detection when a user copy and a bundled template share an
identifier without an explicit `override: true` label.

**Acceptance:**

- 3.13.1 - Convention documented. behavior: "`src/gobby/install/shared/CLAUDE.md` describes gobby-tagged immutability and the user override convention".
- 3.13.2 - Conflict detection exists. symbol: `gobby.workflows.loader.detect_override_conflict`.
- 3.13.3 - Conflict-without-override-label fails loud. test: `tests/workflows/test_loader_overrides.py::test_conflict_without_override_label_fails_loud`.

### 3.14 §2.20 re-expansion gate [category: manual] (depends: 1.9, 2.6, 2.8, 2.10, 3.5, 3.7)

`kind: deliverable`

Target: `.gobby/plans/task-12725-lifecycle-dispatch-rev1.md`

Per rev1 §2.20, run `gobby build` end-to-end against the rev1 plan
itself under the dispatcher built in this plan: plan → expand → dev →
qa → holistic_review → pr → merging cleanly. The build runs in
**unattended mode** with `task.unattended == True` so the
attended-pr_rule escalation does NOT fire and the run can complete
without human PR creation (PR creation is post-0.4.0; rev1 / #12728
follow-up). The bundled dispatcher cron row (3.5) fires the heartbeat;
the merge dispatcher rule (2.10) routes; the merge agent owns terminal
write-back via `mark_task_merged` with optional artifacts (per F5
round-4 — `pr_url` and `merge_sha` may be NULL on the unattended
path). Coverage ledger generated; `gobby plan coverage` reports clean;
configurability spot-check (project-local `qa-reviewer.yaml` override)
is honored without mutating the bundled template.

The merge artifact (`pr_url` / `merge_commit_sha`) belongs to the
EPIC only. Leaves cascade-close to `(merged, closed)` via §1.7's
cascade side effect; per-leaf `task_artifacts.pr_url` remains unset
on every leaf, by design.

**Acceptance:**

- 3.14.1 - Build run produces a clean coverage ledger. behavior: "`gobby plan coverage --plan .gobby/plans/task-12725-lifecycle-dispatch-rev1.md` exits 0".
- 3.14.2 - Unattended dispatcher walks all phases without manual intervention. behavior: "the run executes with `task.unattended == True`, completes with no escalations and no manual lifecycle advances".
- 3.14.3 - Epic reaches `(merged, closed)`; pr_url and merge_commit_sha may be NULL. behavior: "the rev1 epic transitions to `(merged, closed)` via `mark_task_merged`; `task_artifacts.pr_url` and `task_artifacts.merge_commit_sha` may be unset (the unattended path's deferred-PR contract; rev1 / #12728 follow-up)".
- 3.14.4 - Subtree cascade-closes; per-leaf pr_url remains unset. behavior: "every child leaf transitions to `(merged, closed)` per 1.7's cascade side effect; `task_artifacts.pr_url` is unset on every leaf (the merge artifact belongs to the epic only)".
- 3.14.5 - Configurability spot-check passes. behavior: "a project-local override at `.gobby/install/workflows/agents/qa-reviewer.yaml` is honored without mutating the bundled template".

## P6 Phase 4 — Interactive Skill Trios

`kind: framing`

**Goal**: Stand up `/gobby dev`, `/gobby qa`, `/gobby review` so each loop
is exercisable interactively or delegated independent of `gobby build`.
Each trio mirrors the layout of `/gobby plan`, `/gobby expand`, and
`/gobby merge`. Agents are shared with the dispatcher (Phase 2).

### 4.1 /gobby dev skill trio [category: config] (depends: 2.8)

`kind: deliverable`

Target: `src/gobby/install/shared/skills/dev/SKILL.md`,
`src/gobby/install/shared/workflows/dev.yaml`

Mirror the `/gobby plan` skill layout: SKILL.md with I/D opt-in and
per-round handoffs, workflow YAML wiring the developer agent (2.8).
Interactive invocation runs the same agent the dispatcher routes to, so
the loop can be exercised independent of `gobby build`.

**Acceptance:**

- 4.1.1 - SKILL.md exists. file: `src/gobby/install/shared/skills/dev/SKILL.md`.
- 4.1.2 - Workflow YAML exists. file: `src/gobby/install/shared/workflows/dev.yaml`.
- 4.1.3 - I/D opt-in surface present. test: `tests/skills/test_dev_skill.py::test_id_opt_in_present`.
- 4.1.4 - Workflow wires the developer agent. test: `tests/workflows/test_dev_workflow.py::test_developer_agent_wired`.

### 4.2 /gobby qa skill trio [category: config] (depends: 2.4)

`kind: deliverable`

Target: `src/gobby/install/shared/skills/qa/SKILL.md`,
`src/gobby/install/shared/workflows/qa.yaml`

Per-leaf QA loop. Read-only verification surface; QA verdict mirrors the
qa-reviewer agent's contract (approve / reject / escalate).

**Acceptance:**

- 4.2.1 - SKILL.md exists. file: `src/gobby/install/shared/skills/qa/SKILL.md`.
- 4.2.2 - Workflow YAML exists. file: `src/gobby/install/shared/workflows/qa.yaml`.
- 4.2.3 - I/D opt-in surface present. test: `tests/skills/test_qa_skill.py::test_id_opt_in_present`.
- 4.2.4 - Workflow wires the qa-reviewer agent. test: `tests/workflows/test_qa_workflow.py::test_qa_reviewer_wired`.

### 4.3 /gobby review skill trio [category: config] (depends: 2.5)

`kind: deliverable`

Target: `src/gobby/install/shared/skills/review/SKILL.md`,
`src/gobby/install/shared/workflows/review.yaml`

Per-epic holistic review. Three-outcome contract surfaces directly to the
user in interactive mode; in delegated mode the contract drives dispatcher
state.

**Acceptance:**

- 4.3.1 - SKILL.md exists. file: `src/gobby/install/shared/skills/review/SKILL.md`.
- 4.3.2 - Workflow YAML exists. file: `src/gobby/install/shared/workflows/review.yaml`.
- 4.3.3 - I/D opt-in surface present. test: `tests/skills/test_review_skill.py::test_id_opt_in_present`.
- 4.3.4 - Workflow wires the holistic-reviewer agent. test: `tests/workflows/test_review_workflow.py::test_holistic_reviewer_wired`.

## P7 Verification

`kind: verification`

End-to-end gate scoped to the 0.4.0 cut. All six suites green before
sub-plan B is considered complete:

1. **Dispatcher unit tests** — `tests/dispatch/test_rules.py`,
   `tests/dispatch/test_dispatcher.py`, `tests/dispatch/test_actions.py`,
   `tests/dispatch/test_prompts.py` all green; merge rule (2.10) covered.
2. **Per-leaf skill trios** — `tests/skills/test_dev_skill.py`,
   `tests/skills/test_qa_skill.py`, `tests/skills/test_review_skill.py`
   all green.
3. **Build stop via cron toggle** — `tests/build/test_build_stop.py`
   green; cron-row disable on stop, re-enable on resume, lifecycle event
   written, in-flight non-kill, and no `--task` flag exposed all proven.
4. **Yolo rename + composer cap** —
   `tests/build/test_yolo_unattended_split.py` green; storage migration
   round-trip, profile-alias resolution, MCP/HTTP/CLI propagation,
   composer-cap rejection, and `--yolo` no-op all proven.
5. **System cron row** —
   `tests/scheduler/test_dispatch_registration.py` and
   `tests/storage/test_cron.py` green; bundled install,
   `is_system=true` after install, enabled-preserved-on-upgrade, and
   protection helpers (delete refused, toggle/schedule allowed) all
   proven.
6. **§2.20 re-expansion (unattended)** — 3.14 acceptance items pass:
   `gobby build` walks rev1 end-to-end under unattended mode, the
   epic reaches `(merged, closed)` (with `pr_url` and `merge_sha`
   optionally NULL per the deferred-PR contract), every leaf
   cascade-closes to `(merged, closed)` (per-leaf `pr_url` remains
   unset — the merge artifact is epic-only), coverage ledger clean,
   and configurability override is honored without mutating the
   bundled template.

## P8 Plan Changelog

`kind: framing`

- 2026-04-29 — Initial draft authored from approved snapshot
  (`/Users/josh/.claude/plans/wobbly-cuddling-crystal.md`); structure
  conforms to plan-coverage contract grammar (kind annotations, dotted
  acceptance IDs, artifact-kind references). No `## M1 Task Manifest`
  section — adversary writes that on approval.
- 2026-04-29 — Pre-flight fact-check applied (round 1): 1.7 target moved
  from `_review.py` (does not exist) to `_transitions.py` (lines 222 and
  248); 2.4/2.5/2.7 reframed from "create new YAML" to "contract
  alignment + registration" (the YAMLs already ship); 2.8 acknowledges
  the deprecated tombstone at `agents/deprecated/developer.yaml`; 3.6
  retargeted to "document retirement + add guardrail test" (no grandfather
  files exist to remove); 3.7.2 corrected to `update_plan_hash`; 3.9
  reframed and retargeted to per-stage CLI overrides on the existing
  `gobby.config.build.BuildConfig` fields.
- 2026-04-29 — Lifecycle chain updated to insert `research` between
  `ideate` and `architect` (locked-in reference, not implemented); noted
  `/gobby research` will be a multi-modal skill mirroring how
  `impeccable` exposes `craft`/`teach`/`extract` modes.
- 2026-04-29 — Pre-flight round 2 confirmed all current-state claims;
  fixed the impeccable-mode reference from `teach`/consume to
  `craft`/`teach`/`extract` (impeccable's actual modes per
  `src/gobby/install/shared/skills/impeccable/SKILL.md`).
- 2026-04-29 — Round-1 adversary findings folded in (delegated mode):
  **F1** sequencing — split the storage-side rename out as new §1.10 in
  Phase 1 (migration, Task model, cascade helpers, state_semantics);
  reframed §3.1 as the build/CLI/MCP/HTTP surface follow-on (depends:
  1.10); §1.1 now `(depends: 1.10)`. **F2** traceability — §2.1
  acceptance items dropped the backend-developer fallback and now assert
  parser-fail-loud on missing manifest entries (consistent with sub-plan
  A's manifest contract). **F3** missing merge — added §2.9 (merge agent
  contract alignment + registration for existing
  `merge-orchestrator.yaml` and `merge-worker.yaml`) and §2.10 (merge
  dispatcher rule); §3.14 (was §3.11) now depends on 2.10. **F4**
  incomplete yolo→unattended target list — §1.10 enumerates every
  storage-side surface (model, cascade helpers, state_semantics, migration);
  §3.1 enumerates every user-facing surface (BuildOptions, BuildConfig
  profiles with backward-compat aliases, MCP build_task schema, HTTP
  BuildRequest) with per-surface acceptance items. **F5** stop ledger
  data-loss — replaced the per-task `allow_automation` flip with a
  project-wide cron-row toggle: new §3.4 adds `is_system: bool` column
  to `cron_jobs`, §3.5 installs the bundled dispatcher cron row at
  startup with `is_system=true` (preserving operator-set `enabled`
  across upgrades), §3.6 adds protection helpers (delete refused,
  toggle/schedule allowed), §3.7 (reframed from old §3.4) wraps the
  cron toggle as `gobby build stop`/`resume` and drops the `--task`
  subtree flag entirely (post-0.4.0). **§1.9 reframe** — the cron handler
  no longer lives in a `scheduler/handlers/` module (none exists in the
  codebase); §1.9 wires `run_heartbeat` as a cron-invocable action that
  the existing executor dispatches to, with the bundled row in §3.5.
  Total deliverables: 31 → 37 (Phase 1: 9 → 10; Phase 2: 8 → 10;
  Phase 3: 11 → 14; Phase 4: 3).
- 2026-04-29 — Pre-flight round 2 cleanup: 1.10.3 acceptance now
  explicitly covers reads AND SQL writes (cascade `UPDATE ... SET yolo
  = ?` around `_crud.py:375`); 3.6 + 3.7 use the actual
  `CronJobStorage.{delete_job, update_job, toggle_job}` storage method
  names instead of the `*_cron_job` MCP tool surface (the MCP tools
  call through to the storage methods).
- 2026-04-29 — Round-2 adversary findings folded in (delegated mode):
  **F1** §1.6 self-containment — expanded with the explicit ordered rule
  roster (9 rules), each named with predicate + action + cap source;
  added skip-stage / unattended-fallback / retry-cap-read semantics;
  enumerated per-rule acceptance items so an expansion agent seeing only
  §1.6 can implement every rule. **F2** §1.7 lifecycle contract —
  expanded with the full transition matrix (every from-tuple →
  to-tuple + side effects, including QA-leaf parking at
  `(holistic_review, review_approved)`, holistic-rejection's
  `cited_subtasks` reset, expansion artifact resets, merge cascade-
  close); added `advance_lifecycle` helper and extended `de_escalate_task`
  signature; added validation gate sets per (lifecycle, status) tuple.
  **F3** dispatch primitives — §1.2 retargeted from new module to extending
  the existing `src/gobby/storage/tasks/_dispatch_mutex.py` (storage
  helper already exists; `TaskDispatchMutexManager` already defined; just
  needs `attach_run_id`, canonical migration, startup sweep); split out
  new §1.2a runtime mutex wrapper at `src/gobby/dispatch/mutex.py`
  (token-scoped detach, `force_release_for_run`); §1.3 expanded to cover
  the normal claim/end-agent path in addition to terminal/crash/reopen;
  §1.8 expanded with `list_automation_candidates` /
  `is_blocked_by_deps` helpers, TOCTOU re-evaluation under the lease,
  action-kind dispatch behavior (Spawn vs Advance vs Audit vs Escalate
  vs CreateIsolation). **F4** `_kick_dispatcher_tick` bypass —
  §3.7 now requires the kick to honor the dispatcher cron row's
  `enabled` flag so `gobby build stop` followed by `gobby build`
  cannot fire dispatch; both the cron heartbeat and the kick share the
  same enabled-check. **F5** JSON field collision — §3.1/§3.2 split the
  composer-stub flag into a distinct `composer_yolo` field on JSON
  surfaces (BuildOptions, BuildRequest, MCP schema); legacy `yolo` JSON
  field remains a deprecated alias for `unattended` only; the `--yolo`
  CLI flag maps to `composer_yolo`, not `unattended`. **F6** merge
  lifecycle drift — §2.10 corrected to route the existing
  `Lifecycle.merging` enum value (not the non-existent `ready-to-merge`);
  the rule no longer emits `AdvanceLifecycleAction` (merge agent owns
  terminal write-back via 1.7's `advance_lifecycle`); §3.14 reframed
  to cascade-close on the epic with `pr_url` artifact, not per-leaf
  merged. **§1.10 follow-up:** Lifecycle/status enum alignment for
  kanban visibility filed as deferred follow-up #13482 (pre-0.4.0,
  outside this plan). Total deliverables: 37 → 38 (Phase 1: 10 → 11;
  Phase 2: 10; Phase 3: 14; Phase 4: 3).
- 2026-04-29 — Round-3 adversary findings folded in (delegated mode):
  **F1** plan_review tuple sequencing — `plan_review_rule` (§1.6 rule 1)
  retargeted from `(open, open)` to `(plan_review, open)` so it
  matches the live build pipeline's seed lifecycle for plan-file
  builds; §1.7's transition matrix retargeted so post-build rejection
  paths route to `(plan_review, open)` instead of `(open, open)`
  (test_arch reject → plan_review for planner revision; expansion
  failure stays in `expanding` until escalation), and `(open, open)`
  is now reserved exclusively for pre-build/backlog state with an
  invariant test (1.7.11) proving no matrix transition produces it
  post-build. **F2** merge agent terminal write-back surface — added
  `mark_task_merged` and `mark_task_merge_failed` helpers to §1.7
  (constrained agent-facing transitions that call through to
  `advance_lifecycle`), and registered them as gobby-tasks MCP tools
  in §2.9 with merge-orchestrator/merge-worker allowlist updates so
  the YAML merge agents have an actual tool surface to call (not a
  Python helper they can't reach). §2.10 contract updated to point at
  the new tools. **F3** missing isolation-prep rule — inserted
  `isolation_rule` at position 4 in §1.6's roster
  (between expansion_rule and dev_rule); fires on
  `(in_development, open)` for leaves when
  `task_artifacts.isolation in {"worktree", "clone"}` AND the
  artifact pair is absent; emits `CreateIsolationAction`; dev_rule
  predicate updated to require artifact pair present, guaranteeing
  developers never spawn into missing isolation. Rule list grew from
  9 to 10. **F4** expansion-run mutex release gap — extended §1.3
  with three new event handlers (`on_expansion_run_completed`,
  `on_expansion_run_failed`, `on_expansion_run_cancelled`) that each
  call `RuntimeDispatchMutex.force_release_for_run(expansion_run_id)`;
  §2.2 now requires the expansion service to emit the corresponding
  terminal events on every terminal transition; §1.8 documents the
  symmetry (StartExpansionAction lease release is event-driven via
  §1.3, not by the executor). **F5** §1.6 / §2.10 ownership conflict
  on `merge_rule` — §1.6 now owns nine rules + infrastructure (RULES
  list, evaluate, action emission) but explicitly does NOT define
  `merge_rule`; the position-10 entry is reserved with a contract
  description only. §2.10 owns merge_rule's body, registration
  (appends to RULES at position 10), and every merge-rule acceptance
  test. Acceptance 1.6.18 proves merge_rule is not defined in §1.6's
  output. Total deliverables: 38 (no change; isolation_rule is a
  rule entry inside §1.6, not a new deliverable; §1.7 / §2.9 / §2.10
  / §1.3 / §2.2 / §1.8 all expanded in place).
- 2026-04-29 — Round-4 adversary findings folded in (delegated mode):
  **F1** isolation_rule field-source — predicate retargeted from
  `task_artifacts.isolation` (which doesn't carry the requested mode)
  to `task.isolation` (the column on the Task model). `task_artifacts`
  carries only the produced isolation pair (`worktree_path` /
  `worktree_id` or `clone_path` / `clone_id`). 1.6.7/1.6.8 acceptance
  retargeted accordingly. **F2** skip semantics — every skippable
  stage now names its concrete skip target: plan_review →
  (test_arch, open); test_arch → (expanding, open); expanding →
  (in_development, open); qa → (holistic_review, review_approved);
  holistic_review → (pr, open); pr → (merging, open) under
  unattended only. dev and merge are explicitly non-skippable. Build
  service rejects invalid combinations at build time (1.6.21).
  Per-stage skip acceptance items added (1.6.15 through 1.6.20).
  **F3** PR wait state — replaced non-enum `claimed` status with
  `(pr, needs_review)`; matrix updated; 1.7.19 / 1.7.19a / 1.7.19b
  cover the PR-open transition, external-approval transition, and
  the invariant that no transition produces a non-enum status.
  **F4** pr_rule contract — pr_rule no longer dispatches a
  non-existent PR-creation agent. Attended → `EscalateAction` with
  reason `pr_creation_required` for human PR open; unattended →
  deterministic `AdvanceLifecycleAction → (merging, open)` (same as
  stage-:pr skip). Real PR-opening tooling is rev1 / #12728 follow-up.
  Acceptance 1.6.14 + 1.6.14a cover both branches. **F5**
  mark_task_merged optionality — `pr_url` and `merge_sha` are both
  optional on the helper and the MCP tool; clean worktree merge,
  clean clone merge, and skipped-PR/unattended merge all succeed
  without those artifacts (per rev1's #12728 deferral of
  `merge_commit_sha`). Acceptance items 1.7.20 / 20a / 20b / 20c +
  2.9.5 cover the matrix. **F6** §1.6 / §2.10 ownership — replaced
  the temporary "merge_rule symbol absent" assertion with a stable
  invariant pair: §1.6 exports `BASE_RULES` (nine non-merge rules);
  §2.10 exports the final `RULES = [*BASE_RULES, merge_rule]`. The
  dispatcher (1.8) imports `RULES`. Both invariants hold durably in
  the final code. Acceptance 1.6.24 + 2.10.2 enforce. **F7** §1.6
  dependency on §1.10 — §1.6 deps now include §1.1 (which transitively
  depends on §1.10), and rules read the unattended flag through the
  `_is_unattended(task)` helper from §1.1, NOT directly from
  `task.unattended`. The chain 1.6 → 1.1 → 1.10 makes order
  enforceable. Total deliverables: 38 (unchanged; revisions in
  place).
- 2026-04-29 — Round-5 adversary findings folded in (delegated mode):
  **F1** PR-open writer — added `mark_task_pr_opened(task_id,
  pr_url)` helper to §1.7 and registered as a gobby-tasks MCP tool
  in §2.9. The attended pr_rule escalation now resolves through
  this dedicated writer (existing `mark_task_needs_review` does not
  accept pr_url). Acceptance 1.7.2a / 1.7.19 / 2.9.4a / 2.9.4b cover
  the surface. **F2** expansion terminal → advance_lifecycle wiring
  — §1.3 expansion-run handlers now do double duty: release the
  mutex AND call `advance_lifecycle` for the right transition
  (completion → (in_development, open) preserving expansion_run_id;
  failure → (expanding, open) with attempts++ and escalation/
  fallback on exhaust; cancellation → release only, no advance).
  Without this, expansion success would leave the task at
  (expanding, open) and the next heartbeat would re-fire
  expansion_rule. Acceptance 1.3.13 / 1.3.14 / 1.3.14a / 1.3.15 /
  1.3.16 cover. **F3** §3.14 / P7 verification gate — gate now
  explicitly runs in unattended mode, which is the only mode the
  0.4.0 cut can complete end-to-end without human PR creation.
  pr_url and merge_commit_sha are optional/NULL on the unattended
  path. Per-leaf `pr_url` is explicitly unset (the merge artifact
  belongs to the epic only); P7 suite 6 reflects the cascade-close
  semantics. **F4** plan-rejection re-dispatch loop — added
  await-revision guard. plan_review_rule predicate now requires
  EITHER no prior `task_artifacts.last_reviewed_plan_hash` OR
  `plan_hash != last_reviewed_plan_hash`. §1.7's plan-rejected
  side effect writes the rejected hash. The rule cannot re-dispatch
  on an unchanged plan file; it waits for a real revision. Acceptance
  1.7.8 / 1.7.8a / 1.7.8b cover. **F5** AdvanceLifecycleAction
  status-bearing targets — §1.4 action carries `from_lifecycle`,
  `from_status`, `to_lifecycle`, `to_status`. Skip-stage advances,
  leaf-parking, and pr-skip targets all need the status component.
  Acceptance 1.4.4 / 1.4.5 cover. Total deliverables: 38 (unchanged;
  revisions in place).
- 2026-04-29 — Round-6 adversary findings folded in (delegated mode):
  **F1** attended-pr_rule recovery — `mark_task_pr_opened` now
  accepts BOTH `(pr, open)` and `(pr, escalated)` as from-states;
  the latter is the recovery path for the attended-pr_rule's
  `pr_creation_required` escalation. The tool atomically clears
  escalation metadata (`is_escalated=False`, reason cleared),
  writes pr_url, and advances to (pr, needs_review). §1.6 pr_rule
  attended-path doc updated to call out the (pr, escalated)
  intermediate state. Acceptance 1.7.19 / 1.7.19a / 2.9.4b /
  2.9.4c. **F2** terminal-before-attach race — §1.8 dispatcher
  executor allocates `expansion_run_id` and attaches it to the
  mutex BEFORE invoking `start_expansion_run_impl`; §2.2 signature
  accepts caller-allocated run_id. Defense-in-depth: §1.3 handlers
  fall back to release-by-task-id if release-by-run-id finds no
  lease. Synchronous-terminal expansion test (1.8.13b) covers the
  race directly. Acceptance 1.8.13a / 13b / 13c + 2.2.7 / 2.2.8.
  **F3** §3.6 system-row protection allowlist — replaced narrow
  enabled/schedule allow with explicit
  `SYSTEM_ROW_UPDATE_ALLOWED_FIELDS` constant covering enabled +
  the actual schedule fields (schedule_type, cron_expr,
  interval_seconds, run_at, timezone). identity / action fields
  (name, action_type, action_config) explicitly refused with named
  error messages. Acceptance 3.6.1–3.6.8 cover the matrix. Total
  deliverables: 38 (unchanged; revisions in place).
- 2026-04-30 — Round-7 adversary findings folded in (delegated mode):
  **F1** §3.6 protection vs. internal scheduler writes — split
  protection between the operator-facing surface (public
  `update_job` / `delete_job` / rename) and two new caller-scoped
  internal writers: `update_system_job_bookkeeping(...)` for
  scheduler state (`next_run_at`, `last_run_at`, `last_status`,
  `consecutive_failures`) and `reconcile_system_job_definition(...)`
  for startup-only repair of bundled action/schedule fields.
  Internal writers refuse non-system rows, are NOT registered on
  the gobby-cron MCP surface, and are imported directly by
  `CronScheduler` and `gobby.runner.install_dispatcher_cron_row`.
  This preserves the project-wide halt invariant (operators and
  agents cannot rewrite action_type / action_config / bookkeeping
  fields on system rows via the public surface) while letting the
  scheduler's own _check_due_jobs / _execute_and_update advance
  their state and letting startup reconciliation repair bundled
  definitions. §3.5.4 retargeted to call the internal reconciler.
  Acceptance 3.6.6a / 3.6.9 / 3.6.10 / 3.6.11 / 3.6.12 / 3.6.13 /
  3.6.14 / 3.6.15 / 3.6.16 / 3.6.17 cover the new methods, scope
  guards, MCP exclusion, scheduler integration, and toggle-
  recompute path. Total deliverables: 38 (unchanged; revisions in
  place).
- 2026-04-30 — Round-8 adversary findings folded in (delegated mode):
  **F1** §3.5 reconciliation overwriting operator schedule — split
  the startup flow into first-install (seeds bundled action AND
  schedule defaults on a fresh row) vs. upgrade-reconciliation
  (repairs `action_type` / `action_config` only; schedule fields
  are operator-owned once the row exists). `reconcile_system_job_definition`
  signature narrowed to `(job_id, *, action_type, action_config)` —
  no schedule parameters; the helper exits early if action fields
  are already in sync. Acceptance 3.5.4 / 3.5.5 / 3.5.6 / 3.5.7
  cover the install/upgrade split + operator-schedule durability
  + action-config repair without touching operator-set schedule.
  3.6.14a explicitly proves the reconciler does not touch schedule
  fields. Total deliverables: 38 (unchanged; revisions in place).
- 2026-04-30 — Round-9 adversary findings folded in (delegated mode):
  **F1** update_system_job_bookkeeping omitted-vs-null ambiguity —
  signature now uses an `UNSET` sentinel (private module-level
  singleton in `src/gobby/storage/cron.py`) so omitted fields are
  preserved on the existing row and explicit `None` writes a real
  NULL. Required because the scheduler must be able to clear
  `next_run_at = NULL` on disabled / one-shot paths WITHOUT
  clobbering `last_run_at` / `last_status` / `consecutive_failures`,
  and `_execute_and_update` must write telemetry WITHOUT touching
  `next_run_at`. Implementation filters `UNSET` from the SQL UPDATE
  column list; `None` becomes a real NULL write. Acceptance
  3.6.11a (next_run_at-only preserves telemetry), 3.6.11b
  (telemetry-only preserves next_run_at), 3.6.11c (explicit None
  writes NULL without clobbering others), 3.6.11d (UNSET sentinel
  exists). Total deliverables: 38 (unchanged; revisions in place).
- 2026-04-30 — Round-10 adversary findings folded in (delegated mode):
  **F1** target_branch + base_commit_sha contract preservation —
  §1.8 CreateIsolationAction now writes the artifact pair AND
  `base_commit_sha` atomically (the live task_artifacts storage
  raises `MissingIsolationBaseError` when worktree_path/clone_path
  is set without a base). The merge base is computed from
  `task_artifacts.target_branch` using the existing isolation
  handlers' helper. §3.1 preserves rev1's `BuildOptions.target_branch`
  contract across the yolo→unattended rename: `target_branch=None`
  resolves to current branch on plan-file/epic builds; explicit
  values are validated; the resolved branch is persisted on
  `task_artifacts.target_branch` BEFORE any CreateIsolationAction
  emits; leaf builds inherit via cascade. §3.2 wires the
  `--target-branch` CLI flag. Acceptance 1.8.14 / 14a / 14b
  (atomic write + target_branch resolution + missing-target-branch
  escalation), 3.1.10–3.1.14 (target_branch preserved, resolution,
  validation, persistence-before-isolation, leaf cascade), 3.2.5
  (flag mapping). Total deliverables: 38 (unchanged; revisions in
  place).
- 2026-04-30 — Round-11 adversary findings folded in (delegated mode):
  **F1** generated-leaf target_branch inheritance — §2.3 expansion
  apply now copies parent's `task_artifacts.target_branch` onto every
  generated leaf at task-creation time. Without this, plan-file
  builds (which create leaves AFTER the build-time cascade) hit the
  missing-target-branch escalation in §1.8's CreateIsolationAction.
  Expansion is the durable inheritance point because it is the
  moment new leaves are written. Acceptance 2.3.5 / 2.3.6 cover the
  apply-side propagation and the end-to-end plan-file build path.
  **F2** §2.2 surface drift — retargeted from
  `src/gobby/tasks/expansion_service.py` to the canonical
  `src/gobby/mcp_proxy/tools/tasks/_expansion.py` MCP-handler
  module. Signature follows rev1 §2.8b's dependency-injected form:
  `(task_manager, llm_service, config, completion_registry,
  triggering_session_id, task_id, plan_file=None, auto_apply=False,
  force_new=False, provider=None, model=None, project=None,
  run_id=None)`. The dispatcher and MCP closure share this impl;
  no second `ExpansionService.start_run` wrapper exists. §1.8
  `StartExpansionAction` updated to call from
  `gobby.mcp_proxy.tools.tasks._expansion` with the dispatcher's
  service container. Acceptance 2.2.1 / 2.2.1a / 2.2.9 cover the
  retargeting and the no-second-wrapper invariant. Total
  deliverables: 38 (unchanged; revisions in place).
- 2026-04-30 — Round-12 adversary findings folded in (delegated mode):
  **F1** auto_apply pin on dispatcher's StartExpansionAction —
  §1.8 now explicitly pins `auto_apply=True` on every
  dispatcher-triggered call to `start_expansion_run_impl`,
  regardless of the impl's default. Compile-only runs produce no
  children / no covers labels; without the pin, expansion
  completion would advance the parent to `(in_development, open)`
  against an empty subtree and dev_rule would never fire. As
  defense-in-depth, §1.3's `on_expansion_run_completed` handler
  refuses to call `advance_lifecycle` when the run completed
  compile-only (no leaves applied) — independent of the pin, so
  any future caller that forgets the pin is still safe. Acceptance
  1.8.13d (dispatcher pins auto_apply) + 1.3.13a (compile-only
  completion does NOT advance lifecycle). Total deliverables: 38
  (unchanged; revisions in place).
- 2026-04-30 — Round-13 adversary findings folded in (delegated mode):
  **F1** missing parent-epic transition from (in_development, open)
  to (holistic_review, open) — added `all_leaves_holistic_rule`
  at §1.6 position 8. Fires on epics at `(in_development, open)`
  when every child leaf is parked at
  `(holistic_review, review_approved)` or terminal (closed,
  escalated, merged). Emits
  `AdvanceLifecycleAction → (holistic_review, open)`. Without
  this rule, the parent epic stranded after every leaf finished QA
  because `leaf_park_rule` only moves leaves and `holistic_rule`
  only fires on epics already at `(holistic_review, open)`. Rule
  list grew 10 → 11; BASE_RULES grew 9 → 10; final RULES (in
  §2.10) is now BASE_RULES + [merge_rule] with merge_rule at
  position 11. §1.7 transition matrix gained the corresponding
  bookkeeping entry. Acceptance 1.6.12a / 1.6.12b / 1.6.12c +
  1.7.16a + 2.10.2 (renamed to position-11 invariant). Total
  deliverables: 38 (unchanged; revisions in place).
- 2026-04-30 — Round-14 adversary findings folded in (delegated mode):
  **F1** stale 1.6.24 acceptance — the BASE_RULES count assertion
  was still pinned at "nine entries" from round 12, contradicting
  round 13's growth to ten. Updated 1.6.24 to assert exactly ten
  entries with `all_leaves_holistic_rule` at position 8 and
  `merge_rule` excluded. Test renamed to
  `test_base_rules_has_ten_entries_with_all_leaves_holistic_at_position_8_and_excludes_merge_rule`.
  Total deliverables: 38 (unchanged; revisions in place).

## M1 Task Manifest

`kind: manifest`

```yaml
- title: "Task model dispatch helpers"
  category: code
  task_type: task
  depends_on: ["1.10"]
  validation_criteria: "Acceptance for Task model dispatch helpers is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.1:1.1.1", "covers:sub-plan-b-dispatcher:1.1:1.1.2", "covers:sub-plan-b-dispatcher:1.1:1.1.3", "covers:sub-plan-b-dispatcher:1.1:1.1.4"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.1"
- title: "Per-task dispatch mutex storage alignment"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Per-task dispatch mutex storage alignment is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.2:1.2.1", "covers:sub-plan-b-dispatcher:1.2:1.2.2", "covers:sub-plan-b-dispatcher:1.2:1.2.3", "covers:sub-plan-b-dispatcher:1.2:1.2.4", "covers:sub-plan-b-dispatcher:1.2:1.2.5", "covers:sub-plan-b-dispatcher:1.2:1.2.6", "covers:sub-plan-b-dispatcher:1.2:1.2.7", "covers:sub-plan-b-dispatcher:1.2:1.2.8"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.2"
- title: "Runtime dispatch mutex wrapper"
  category: code
  task_type: task
  depends_on: ["1.2"]
  validation_criteria: "Acceptance for Runtime dispatch mutex wrapper is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.2a:1.2a.1", "covers:sub-plan-b-dispatcher:1.2a:1.2a.2", "covers:sub-plan-b-dispatcher:1.2a:1.2a.3", "covers:sub-plan-b-dispatcher:1.2a:1.2a.4", "covers:sub-plan-b-dispatcher:1.2a:1.2a.5", "covers:sub-plan-b-dispatcher:1.2a:1.2a.6"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.2a"
- title: "Mutex-clearing event handlers"
  category: code
  task_type: task
  depends_on: ["1.2", "1.2a"]
  validation_criteria: "Acceptance for Mutex-clearing event handlers is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.3:1.3.1", "covers:sub-plan-b-dispatcher:1.3:1.3.2", "covers:sub-plan-b-dispatcher:1.3:1.3.3", "covers:sub-plan-b-dispatcher:1.3:1.3.4", "covers:sub-plan-b-dispatcher:1.3:1.3.5", "covers:sub-plan-b-dispatcher:1.3:1.3.6", "covers:sub-plan-b-dispatcher:1.3:1.3.7", "covers:sub-plan-b-dispatcher:1.3:1.3.8", "covers:sub-plan-b-dispatcher:1.3:1.3.9", "covers:sub-plan-b-dispatcher:1.3:1.3.10", "covers:sub-plan-b-dispatcher:1.3:1.3.11", "covers:sub-plan-b-dispatcher:1.3:1.3.12", "covers:sub-plan-b-dispatcher:1.3:1.3.13", "covers:sub-plan-b-dispatcher:1.3:1.3.13a", "covers:sub-plan-b-dispatcher:1.3:1.3.14", "covers:sub-plan-b-dispatcher:1.3:1.3.14a", "covers:sub-plan-b-dispatcher:1.3:1.3.15", "covers:sub-plan-b-dispatcher:1.3:1.3.16"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.3"
- title: "Dispatch action types"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Dispatch action types is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.4:1.4.1", "covers:sub-plan-b-dispatcher:1.4:1.4.2", "covers:sub-plan-b-dispatcher:1.4:1.4.3", "covers:sub-plan-b-dispatcher:1.4:1.4.4", "covers:sub-plan-b-dispatcher:1.4:1.4.5", "covers:sub-plan-b-dispatcher:1.4:1.4.6"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.4"
- title: "Prompt-builder registry"
  category: code
  task_type: task
  depends_on: ["1.4"]
  validation_criteria: "Acceptance for Prompt-builder registry is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.5:1.5.1", "covers:sub-plan-b-dispatcher:1.5:1.5.2", "covers:sub-plan-b-dispatcher:1.5:1.5.3", "covers:sub-plan-b-dispatcher:1.5:1.5.4"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.5"
- title: "Ordered decision rules"
  category: code
  task_type: task
  depends_on: ["1.1", "1.4", "1.5"]
  validation_criteria: "Acceptance for Ordered decision rules is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.6:1.6.1", "covers:sub-plan-b-dispatcher:1.6:1.6.2", "covers:sub-plan-b-dispatcher:1.6:1.6.3", "covers:sub-plan-b-dispatcher:1.6:1.6.4", "covers:sub-plan-b-dispatcher:1.6:1.6.5", "covers:sub-plan-b-dispatcher:1.6:1.6.6", "covers:sub-plan-b-dispatcher:1.6:1.6.7", "covers:sub-plan-b-dispatcher:1.6:1.6.8", "covers:sub-plan-b-dispatcher:1.6:1.6.9", "covers:sub-plan-b-dispatcher:1.6:1.6.10", "covers:sub-plan-b-dispatcher:1.6:1.6.11", "covers:sub-plan-b-dispatcher:1.6:1.6.12", "covers:sub-plan-b-dispatcher:1.6:1.6.12a", "covers:sub-plan-b-dispatcher:1.6:1.6.12b", "covers:sub-plan-b-dispatcher:1.6:1.6.12c", "covers:sub-plan-b-dispatcher:1.6:1.6.13", "covers:sub-plan-b-dispatcher:1.6:1.6.14", "covers:sub-plan-b-dispatcher:1.6:1.6.14a", "covers:sub-plan-b-dispatcher:1.6:1.6.15", "covers:sub-plan-b-dispatcher:1.6:1.6.16", "covers:sub-plan-b-dispatcher:1.6:1.6.17", "covers:sub-plan-b-dispatcher:1.6:1.6.18", "covers:sub-plan-b-dispatcher:1.6:1.6.19", "covers:sub-plan-b-dispatcher:1.6:1.6.20", "covers:sub-plan-b-dispatcher:1.6:1.6.21", "covers:sub-plan-b-dispatcher:1.6:1.6.22", "covers:sub-plan-b-dispatcher:1.6:1.6.23", "covers:sub-plan-b-dispatcher:1.6:1.6.24"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.6"
- title: "Lifecycle transitions in review tools"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Lifecycle transitions in review tools is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.7:1.7.1", "covers:sub-plan-b-dispatcher:1.7:1.7.2", "covers:sub-plan-b-dispatcher:1.7:1.7.2a", "covers:sub-plan-b-dispatcher:1.7:1.7.3", "covers:sub-plan-b-dispatcher:1.7:1.7.4", "covers:sub-plan-b-dispatcher:1.7:1.7.4a", "covers:sub-plan-b-dispatcher:1.7:1.7.4b", "covers:sub-plan-b-dispatcher:1.7:1.7.5", "covers:sub-plan-b-dispatcher:1.7:1.7.6", "covers:sub-plan-b-dispatcher:1.7:1.7.7", "covers:sub-plan-b-dispatcher:1.7:1.7.8", "covers:sub-plan-b-dispatcher:1.7:1.7.8a", "covers:sub-plan-b-dispatcher:1.7:1.7.8b", "covers:sub-plan-b-dispatcher:1.7:1.7.9", "covers:sub-plan-b-dispatcher:1.7:1.7.10", "covers:sub-plan-b-dispatcher:1.7:1.7.11", "covers:sub-plan-b-dispatcher:1.7:1.7.12", "covers:sub-plan-b-dispatcher:1.7:1.7.13", "covers:sub-plan-b-dispatcher:1.7:1.7.14", "covers:sub-plan-b-dispatcher:1.7:1.7.15", "covers:sub-plan-b-dispatcher:1.7:1.7.16", "covers:sub-plan-b-dispatcher:1.7:1.7.16a", "covers:sub-plan-b-dispatcher:1.7:1.7.17", "covers:sub-plan-b-dispatcher:1.7:1.7.18", "covers:sub-plan-b-dispatcher:1.7:1.7.19", "covers:sub-plan-b-dispatcher:1.7:1.7.19a", "covers:sub-plan-b-dispatcher:1.7:1.7.19b", "covers:sub-plan-b-dispatcher:1.7:1.7.19c", "covers:sub-plan-b-dispatcher:1.7:1.7.20", "covers:sub-plan-b-dispatcher:1.7:1.7.20a", "covers:sub-plan-b-dispatcher:1.7:1.7.20b", "covers:sub-plan-b-dispatcher:1.7:1.7.20c", "covers:sub-plan-b-dispatcher:1.7:1.7.21", "covers:sub-plan-b-dispatcher:1.7:1.7.22", "covers:sub-plan-b-dispatcher:1.7:1.7.23", "covers:sub-plan-b-dispatcher:1.7:1.7.24"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.7"
- title: "Dispatcher scanner"
  category: code
  task_type: task
  depends_on: ["1.2", "1.2a", "1.6"]
  validation_criteria: "Acceptance for Dispatcher scanner is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.8:1.8.1", "covers:sub-plan-b-dispatcher:1.8:1.8.2", "covers:sub-plan-b-dispatcher:1.8:1.8.3", "covers:sub-plan-b-dispatcher:1.8:1.8.4", "covers:sub-plan-b-dispatcher:1.8:1.8.5", "covers:sub-plan-b-dispatcher:1.8:1.8.6", "covers:sub-plan-b-dispatcher:1.8:1.8.7", "covers:sub-plan-b-dispatcher:1.8:1.8.8", "covers:sub-plan-b-dispatcher:1.8:1.8.9", "covers:sub-plan-b-dispatcher:1.8:1.8.10", "covers:sub-plan-b-dispatcher:1.8:1.8.11", "covers:sub-plan-b-dispatcher:1.8:1.8.12", "covers:sub-plan-b-dispatcher:1.8:1.8.13", "covers:sub-plan-b-dispatcher:1.8:1.8.13a", "covers:sub-plan-b-dispatcher:1.8:1.8.13b", "covers:sub-plan-b-dispatcher:1.8:1.8.13c", "covers:sub-plan-b-dispatcher:1.8:1.8.13d", "covers:sub-plan-b-dispatcher:1.8:1.8.14", "covers:sub-plan-b-dispatcher:1.8:1.8.14a", "covers:sub-plan-b-dispatcher:1.8:1.8.14b", "covers:sub-plan-b-dispatcher:1.8:1.8.15", "covers:sub-plan-b-dispatcher:1.8:1.8.16"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.8"
- title: "Dispatcher cron action handler"
  category: code
  task_type: task
  depends_on: ["1.8"]
  validation_criteria: "Acceptance for Dispatcher cron action handler is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.9:1.9.1", "covers:sub-plan-b-dispatcher:1.9:1.9.2"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.9"
- title: "Yolo → unattended storage core rename"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Yolo → unattended storage core rename is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:1.10:1.10.1", "covers:sub-plan-b-dispatcher:1.10:1.10.2", "covers:sub-plan-b-dispatcher:1.10:1.10.3", "covers:sub-plan-b-dispatcher:1.10:1.10.4", "covers:sub-plan-b-dispatcher:1.10:1.10.5", "covers:sub-plan-b-dispatcher:1.10:1.10.6"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "1.10"
- title: "Expansion agent selection"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Expansion agent selection is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.1:2.1.1", "covers:sub-plan-b-dispatcher:2.1:2.1.2", "covers:sub-plan-b-dispatcher:2.1:2.1.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.1"
- title: "In-process expansion start"
  category: code
  task_type: task
  depends_on: ["2.1"]
  validation_criteria: "Acceptance for In-process expansion start is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.2:2.2.1", "covers:sub-plan-b-dispatcher:2.2:2.2.1a", "covers:sub-plan-b-dispatcher:2.2:2.2.2", "covers:sub-plan-b-dispatcher:2.2:2.2.3", "covers:sub-plan-b-dispatcher:2.2:2.2.4", "covers:sub-plan-b-dispatcher:2.2:2.2.5", "covers:sub-plan-b-dispatcher:2.2:2.2.6", "covers:sub-plan-b-dispatcher:2.2:2.2.7", "covers:sub-plan-b-dispatcher:2.2:2.2.8", "covers:sub-plan-b-dispatcher:2.2:2.2.9"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.2"
- title: "Expansion service split"
  category: refactor
  task_type: task
  depends_on: ["2.2"]
  validation_criteria: "Acceptance for Expansion service split is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.3:2.3.1", "covers:sub-plan-b-dispatcher:2.3:2.3.2", "covers:sub-plan-b-dispatcher:2.3:2.3.3", "covers:sub-plan-b-dispatcher:2.3:2.3.4", "covers:sub-plan-b-dispatcher:2.3:2.3.5", "covers:sub-plan-b-dispatcher:2.3:2.3.6"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.3"
- title: "qa-reviewer contract alignment + registration"
  category: config
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for qa-reviewer contract alignment + registration is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.4:2.4.1", "covers:sub-plan-b-dispatcher:2.4:2.4.2", "covers:sub-plan-b-dispatcher:2.4:2.4.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.4"
- title: "holistic-reviewer contract alignment + registration"
  category: config
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for holistic-reviewer contract alignment + registration is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.5:2.5.1", "covers:sub-plan-b-dispatcher:2.5:2.5.2", "covers:sub-plan-b-dispatcher:2.5:2.5.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.5"
- title: "expansion-qa harness"
  category: code
  task_type: task
  depends_on: ["2.3"]
  validation_criteria: "Acceptance for expansion-qa harness is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.6:2.6.1", "covers:sub-plan-b-dispatcher:2.6:2.6.2", "covers:sub-plan-b-dispatcher:2.6:2.6.3", "covers:sub-plan-b-dispatcher:2.6:2.6.4", "covers:sub-plan-b-dispatcher:2.6:2.6.5"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.6"
- title: "test-architect contract alignment + registration"
  category: config
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for test-architect contract alignment + registration is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.7:2.7.1", "covers:sub-plan-b-dispatcher:2.7:2.7.2"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.7"
- title: "developer agent (active root)"
  category: config
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for developer agent (active root) is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.8:2.8.1", "covers:sub-plan-b-dispatcher:2.8:2.8.2", "covers:sub-plan-b-dispatcher:2.8:2.8.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.8"
- title: "Merge agents contract alignment + registration"
  category: config
  task_type: task
  depends_on: ["1.7"]
  validation_criteria: "Acceptance for Merge agents contract alignment + registration is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.9:2.9.1", "covers:sub-plan-b-dispatcher:2.9:2.9.2", "covers:sub-plan-b-dispatcher:2.9:2.9.3", "covers:sub-plan-b-dispatcher:2.9:2.9.4", "covers:sub-plan-b-dispatcher:2.9:2.9.4a", "covers:sub-plan-b-dispatcher:2.9:2.9.4b", "covers:sub-plan-b-dispatcher:2.9:2.9.4c", "covers:sub-plan-b-dispatcher:2.9:2.9.5", "covers:sub-plan-b-dispatcher:2.9:2.9.6", "covers:sub-plan-b-dispatcher:2.9:2.9.7", "covers:sub-plan-b-dispatcher:2.9:2.9.8", "covers:sub-plan-b-dispatcher:2.9:2.9.9", "covers:sub-plan-b-dispatcher:2.9:2.9.10", "covers:sub-plan-b-dispatcher:2.9:2.9.11"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.9"
- title: "Merge dispatcher rule"
  category: code
  task_type: task
  depends_on: ["1.6", "2.9"]
  validation_criteria: "Acceptance for Merge dispatcher rule is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:2.10:2.10.1", "covers:sub-plan-b-dispatcher:2.10:2.10.2", "covers:sub-plan-b-dispatcher:2.10:2.10.3", "covers:sub-plan-b-dispatcher:2.10:2.10.4", "covers:sub-plan-b-dispatcher:2.10:2.10.5", "covers:sub-plan-b-dispatcher:2.10:2.10.6", "covers:sub-plan-b-dispatcher:2.10:2.10.7"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "2.10"
- title: "Yolo → unattended build/CLI/MCP/HTTP surface"
  category: code
  task_type: task
  depends_on: ["1.10"]
  validation_criteria: "Acceptance for Yolo → unattended build/CLI/MCP/HTTP surface is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.1:3.1.1", "covers:sub-plan-b-dispatcher:3.1:3.1.2", "covers:sub-plan-b-dispatcher:3.1:3.1.3", "covers:sub-plan-b-dispatcher:3.1:3.1.4", "covers:sub-plan-b-dispatcher:3.1:3.1.5", "covers:sub-plan-b-dispatcher:3.1:3.1.6", "covers:sub-plan-b-dispatcher:3.1:3.1.7", "covers:sub-plan-b-dispatcher:3.1:3.1.8", "covers:sub-plan-b-dispatcher:3.1:3.1.9", "covers:sub-plan-b-dispatcher:3.1:3.1.10", "covers:sub-plan-b-dispatcher:3.1:3.1.11", "covers:sub-plan-b-dispatcher:3.1:3.1.12", "covers:sub-plan-b-dispatcher:3.1:3.1.13", "covers:sub-plan-b-dispatcher:3.1:3.1.14"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.1"
- title: "CLI flags: --unattended and --yolo"
  category: code
  task_type: task
  depends_on: ["3.1"]
  validation_criteria: "Acceptance for CLI flags: --unattended and --yolo is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.2:3.2.1", "covers:sub-plan-b-dispatcher:3.2:3.2.2", "covers:sub-plan-b-dispatcher:3.2:3.2.3", "covers:sub-plan-b-dispatcher:3.2:3.2.4", "covers:sub-plan-b-dispatcher:3.2:3.2.5"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.2"
- title: "Composer-scope cap"
  category: code
  task_type: task
  depends_on: ["3.2"]
  validation_criteria: "Acceptance for Composer-scope cap is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.3:3.3.1", "covers:sub-plan-b-dispatcher:3.3:3.3.2", "covers:sub-plan-b-dispatcher:3.3:3.3.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.3"
- title: "Cron is_system column migration"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Cron is_system column migration is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.4:3.4.1", "covers:sub-plan-b-dispatcher:3.4:3.4.2", "covers:sub-plan-b-dispatcher:3.4:3.4.3", "covers:sub-plan-b-dispatcher:3.4:3.4.4"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.4"
- title: "Bundled dispatcher cron row install"
  category: code
  task_type: task
  depends_on: ["3.4", "1.9"]
  validation_criteria: "Acceptance for Bundled dispatcher cron row install is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.5:3.5.1", "covers:sub-plan-b-dispatcher:3.5:3.5.2", "covers:sub-plan-b-dispatcher:3.5:3.5.3", "covers:sub-plan-b-dispatcher:3.5:3.5.4", "covers:sub-plan-b-dispatcher:3.5:3.5.5", "covers:sub-plan-b-dispatcher:3.5:3.5.6", "covers:sub-plan-b-dispatcher:3.5:3.5.7"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.5"
- title: "System cron row protection helpers"
  category: code
  task_type: task
  depends_on: ["3.4"]
  validation_criteria: "Acceptance for System cron row protection helpers is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.6:3.6.1", "covers:sub-plan-b-dispatcher:3.6:3.6.2", "covers:sub-plan-b-dispatcher:3.6:3.6.3", "covers:sub-plan-b-dispatcher:3.6:3.6.4", "covers:sub-plan-b-dispatcher:3.6:3.6.5", "covers:sub-plan-b-dispatcher:3.6:3.6.6", "covers:sub-plan-b-dispatcher:3.6:3.6.6a", "covers:sub-plan-b-dispatcher:3.6:3.6.7", "covers:sub-plan-b-dispatcher:3.6:3.6.8", "covers:sub-plan-b-dispatcher:3.6:3.6.9", "covers:sub-plan-b-dispatcher:3.6:3.6.10", "covers:sub-plan-b-dispatcher:3.6:3.6.11", "covers:sub-plan-b-dispatcher:3.6:3.6.11a", "covers:sub-plan-b-dispatcher:3.6:3.6.11b", "covers:sub-plan-b-dispatcher:3.6:3.6.11c", "covers:sub-plan-b-dispatcher:3.6:3.6.11d", "covers:sub-plan-b-dispatcher:3.6:3.6.12", "covers:sub-plan-b-dispatcher:3.6:3.6.13", "covers:sub-plan-b-dispatcher:3.6:3.6.14", "covers:sub-plan-b-dispatcher:3.6:3.6.14a", "covers:sub-plan-b-dispatcher:3.6:3.6.15", "covers:sub-plan-b-dispatcher:3.6:3.6.16", "covers:sub-plan-b-dispatcher:3.6:3.6.17"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.6"
- title: "gobby build stop / resume via cron toggle"
  category: code
  task_type: task
  depends_on: ["3.5", "3.6"]
  validation_criteria: "Acceptance for gobby build stop / resume via cron toggle is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.7:3.7.1", "covers:sub-plan-b-dispatcher:3.7:3.7.2", "covers:sub-plan-b-dispatcher:3.7:3.7.3", "covers:sub-plan-b-dispatcher:3.7:3.7.4", "covers:sub-plan-b-dispatcher:3.7:3.7.5", "covers:sub-plan-b-dispatcher:3.7:3.7.6", "covers:sub-plan-b-dispatcher:3.7:3.7.7", "covers:sub-plan-b-dispatcher:3.7:3.7.8", "covers:sub-plan-b-dispatcher:3.7:3.7.9", "covers:sub-plan-b-dispatcher:3.7:3.7.10"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.7"
- title: "Hook/rule scoping for build agents"
  category: config
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Hook/rule scoping for build agents is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.8:3.8.1", "covers:sub-plan-b-dispatcher:3.8:3.8.2", "covers:sub-plan-b-dispatcher:3.8:3.8.3", "covers:sub-plan-b-dispatcher:3.8:3.8.4"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.8"
- title: "Document grandfather/legacy retirement"
  category: docs
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Document grandfather/legacy retirement is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.9:3.9.1", "covers:sub-plan-b-dispatcher:3.9:3.9.2"]
  assigned_agent: backend-developer
  tdd: false
  source_section: "3.9"
- title: "Coverage manifest lifecycle"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Coverage manifest lifecycle is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.10:3.10.1", "covers:sub-plan-b-dispatcher:3.10:3.10.2", "covers:sub-plan-b-dispatcher:3.10:3.10.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.10"
- title: "Auto-move plans on epic terminal"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Auto-move plans on epic terminal is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.11:3.11.1", "covers:sub-plan-b-dispatcher:3.11:3.11.2", "covers:sub-plan-b-dispatcher:3.11:3.11.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.11"
- title: "CLI overrides for BuildConfig retry caps"
  category: code
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for CLI overrides for BuildConfig retry caps is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.12:3.12.1", "covers:sub-plan-b-dispatcher:3.12:3.12.2", "covers:sub-plan-b-dispatcher:3.12:3.12.3"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "3.12"
- title: "Configurability convention"
  category: docs
  task_type: task
  depends_on: []
  validation_criteria: "Acceptance for Configurability convention is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.13:3.13.1", "covers:sub-plan-b-dispatcher:3.13:3.13.2", "covers:sub-plan-b-dispatcher:3.13:3.13.3"]
  assigned_agent: backend-developer
  tdd: false
  source_section: "3.13"
- title: "§2.20 re-expansion gate"
  category: manual
  task_type: task
  depends_on: ["1.9", "2.6", "2.8", "2.10", "3.5", "3.7"]
  validation_criteria: "Acceptance for §2.20 re-expansion gate is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:3.14:3.14.1", "covers:sub-plan-b-dispatcher:3.14:3.14.2", "covers:sub-plan-b-dispatcher:3.14:3.14.3", "covers:sub-plan-b-dispatcher:3.14:3.14.4", "covers:sub-plan-b-dispatcher:3.14:3.14.5"]
  assigned_agent: backend-developer
  tdd: false
  source_section: "3.14"
- title: "/gobby dev skill trio"
  category: config
  task_type: task
  depends_on: ["2.8"]
  validation_criteria: "Acceptance for /gobby dev skill trio is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:4.1:4.1.1", "covers:sub-plan-b-dispatcher:4.1:4.1.2", "covers:sub-plan-b-dispatcher:4.1:4.1.3", "covers:sub-plan-b-dispatcher:4.1:4.1.4"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "4.1"
- title: "/gobby qa skill trio"
  category: config
  task_type: task
  depends_on: ["2.4"]
  validation_criteria: "Acceptance for /gobby qa skill trio is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:4.2:4.2.1", "covers:sub-plan-b-dispatcher:4.2:4.2.2", "covers:sub-plan-b-dispatcher:4.2:4.2.3", "covers:sub-plan-b-dispatcher:4.2:4.2.4"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "4.2"
- title: "/gobby review skill trio"
  category: config
  task_type: task
  depends_on: ["2.5"]
  validation_criteria: "Acceptance for /gobby review skill trio is satisfied."
  labels: ["covers:sub-plan-b-dispatcher:4.3:4.3.1", "covers:sub-plan-b-dispatcher:4.3:4.3.2", "covers:sub-plan-b-dispatcher:4.3:4.3.3", "covers:sub-plan-b-dispatcher:4.3:4.3.4"]
  assigned_agent: backend-developer
  tdd: true
  source_section: "4.3"
```
