# Epic: Task Lifecycle V2

Task: `gobby-#11666`

## Summary

This epic replaces the overloaded task `status` model with explicit task-state axes while keeping the repo operational during the migration.

The immediate objective is to stabilize incorrect current behavior around claimed review work and de-escalation. The longer objective is to make task meaning expressible without inferring ownership, blocked state, or closure from a single enum.

The canonical end-state for execution work is:

- `claimed_by_session_id`
- `lifecycle_stage: null | in_progress | needs_review | review_approved`
- blocked/escalation state separate from lifecycle
- closed state separate from lifecycle
- legacy `status` retained only as a projected compatibility field during migration

This epic does **not** fold planning or expansion states into execution lifecycle. Those belong on a separate axis.

## Progress

Updated: `2026-04-11`

- Phase 0 is complete in code and validated with focused tests.
- Phase 1 through Phase 6 are not started.
- The implementation slice for Phase 0 was tracked under `gobby-#11668`.

### Phase 0 Landed

- Claimed review work now survives reconciliation and session-start fallback instead of being dropped when it is no longer `in_progress`.
- Stop-gate messaging and enforcement now treat active claimed work as the blocking concept rather than only `in_progress`.
- Orchestrator QA dispatch skips already-claimed `needs_review` tasks.
- Spawn, compact/clear handoff, and web-chat spawn no longer blindly reassign non-open tasks that are already intentionally owned.
- Heartbeat and failed-agent recovery now release stale review-task ownership without regressing lifecycle state.
- De-escalation now accepts an explicit `target_status` across MCP, REST, CLI, and validation paths.
- Phase 0 introduced `src/gobby/tasks/state_semantics.py` as the temporary shared source of truth for transitional claim semantics. Reuse it until canonical ownership lands.

### Phase 0 Validation

- `uv run pytest tests/workflows/test_stop_gates_rules.py tests/workflows/test_hooks.py tests/workflows/test_pipeline_heartbeat.py tests/hooks/test_session_events_coverage.py tests/hooks/test_event_handlers.py -q`
- `uv run pytest tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py tests/mcp_proxy/test_validation_mcp_tools.py -q`
- `uv run pytest tests/servers/routes/test_tasks_routes.py tests/cli/test_validation_cli.py -q`
- `uv run pytest tests/servers/routes/test_agent_spawn_routes.py tests/agents/test_lifecycle_monitor_extra.py -q`
- `uv run ruff check ...`

### Next Agent Start Here

- Start with Phase 1. Do not add more ad hoc status checks in Phase 0-era code paths unless required for a bug fix.
- Build the central task transition/projection layer before introducing more schema dual-writes.
- Add `claimed_by_session_id` and thread it through storage and lifecycle tools while keeping `assignee` compatibility-only.
- Treat `task_claimed` and `claimed_tasks` as derived mirrors from canonical ownership once `claimed_by_session_id` exists.
- Keep the locked semantic decisions intact:
  - only `closed` unblocks dependents
  - closing preserves the last lifecycle stage for audit
  - planning/expansion remains off the execution lifecycle axis

## Locked Decisions

| Decision | Choice | Why |
|---|---|---|
| Canonical ownership | `claimed_by_session_id` | Ownership should be explicit and session-scoped instead of inferred from `status` or overloaded `assignee`. |
| `assignee` in this epic | Keep temporarily as compatibility/routing state | Existing flows still use it for non-canonical routing. Removing it now increases migration risk without helping the core split. |
| Dependency satisfaction | Only `closed` tasks unblock dependents | Review stages are not completion. This fixes the current semantic drift between readiness helpers and workflow completion checks. |
| Close semantics | Preserve the last `lifecycle_stage` underneath close state | Auditability is more valuable than collapsing lifecycle history on close. |
| Planning/expansion workflow | Separate from execution lifecycle | Existing planning/expansion concepts should not pollute build/review/merge semantics. |

## Research Findings

- `status` is hard-coded across storage queries, aggregates, search, REST routes, MCP tools, CLI filters, workflow rules, pipeline scans, background recovery, admin/project stats, JSONL sync, and a large web UI surface.
- Current ownership is split between session-level mirrors (`task_claimed`, `claimed_tasks`) and task fields, while many readers still equate "claimed" with `status='in_progress'`.
- `assignee` is overloaded today for active implementation ownership, QA/orchestrator routing, spawned child-session assignment, and web chat conversation routing.
- Transition logic is duplicated across MCP lifecycle tools, REST routes, CLI commands, task validation helpers, heartbeat recovery, and lifecycle monitoring.
- Current readiness semantics are inconsistent:
  - some queries treat `needs_review` or `review_approved` as dependency-satisfying
  - some counts treat only `open` as ready
  - workflow completion helpers only treat `closed` as complete
- The external compatibility surface is broader than the original draft: API serialization, web hooks/components, JSONL sync, admin health, and project stats still expose or depend on legacy `status`/`assignee`.
- The repo already has a separate expansion axis (`expansion_status`, `expansion_context`), so execution lifecycle does not need another planning-specific state during this epic.

## Target End-State

### Canonical execution fields

- `claimed_by_session_id: str | null`
- `lifecycle_stage: null | in_progress | needs_review | review_approved`
- blocked state represented by escalation/blocker metadata rather than lifecycle stage
- closed state represented independently from lifecycle, preserving the last lifecycle stage for audit

### Derived predicates

- `is_ready`
- `is_blocked`
- `is_closed`
- `is_merge_ready`

### Compatibility fields during migration

- `status` remains as a derived projection for interim callers only
- `assignee` remains temporarily for compatibility/routing, but must no longer be treated as canonical ownership
- session-level claim mirrors remain derived from canonical ownership until they can be removed

## Non-Goals

- Removing `assignee` in this epic
- Encoding planning or expansion phases into execution lifecycle
- Preserving every legacy surface indefinitely
- Broadening the old `status` enum further instead of replacing it

## Delivery Order

1. Phase 0: Nominal Fix and Behavior Stabilization `(complete)`
2. Phase 1: Canonical Transition Layer and Explicit Ownership `(next)`
3. Phase 2: Lifecycle/Blocked/Closed Split and Query Cutover
4. Phase 3: External Interfaces and Serialization
5. Phase 4: Rules, Agents, Pipelines, and Recovery
6. Phase 5: Web UI
7. Phase 6: Future Workflow-Phase Follow-up

## Phase 0: Nominal Fix and Behavior Stabilization

### Goal

Stop the current incorrect behavior without widening the old model further than necessary.

### Status

Complete.

### Atomic tasks

1. Patch turn-end claim reconciliation in `src/gobby/workflows/observers.py` so it does not discard intentionally claimed non-`in_progress` tasks that are still actively owned.
2. Patch stop-gate logic and messaging in `src/gobby/install/shared/workflows/rules/stop-gates/require-task-close.yaml` so blocking is enforced in terms of active claimed work, not only `in_progress`.
3. Patch review-task dispatch logic in `src/gobby/install/shared/workflows/pipelines/orchestrator.yaml` so it skips `needs_review` tasks that are already claimed.
4. Patch spawn and assignment paths that currently set `assignee` on non-open tasks so they preserve intentional ownership and avoid duplicate reassignment:
   - `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`
   - `src/gobby/hooks/event_handlers/_session_start.py`
   - `src/gobby/servers/routes/agent_spawn.py`
5. Unify de-escalation behavior across MCP, CLI, REST, and validation paths so de-escalation accepts an explicit target instead of implicitly returning to incompatible defaults.
6. Patch hidden claim/state readers that still equate claim with `status='in_progress'`, including:
   - `src/gobby/hooks/event_handlers/_session_responses.py`
   - `src/gobby/workflows/pipeline_heartbeat.py`
   - `src/gobby/agents/lifecycle_monitor.py`
7. Add regression coverage for claimed review tasks, de-escalation targets, reassignment prevention, and recovery behavior in the relevant workflow, route, MCP, and hook tests.

### Acceptance criteria

- A claimed task does not disappear from enforcement solely because it is in `needs_review`.
- QA/orchestrator does not pick an already-claimed review task.
- De-escalation behavior is consistent across MCP, REST, CLI, and validation paths.
- Recovery and session-rebuild paths no longer silently drop active claims outside `in_progress`.

## Phase 1: Canonical Transition Layer and Explicit Ownership

### Goal

Make ownership first-class and centralize state transitions before broader schema cutover.

### Atomic tasks

1. Introduce a central task transition/projection layer that owns:
   - claim and unclaim
   - lifecycle transitions
   - escalation and de-escalation
   - close and reopen behavior
   - legacy `status` projection
2. Add `claimed_by_session_id` to the task storage model and dataclass in `src/gobby/storage/tasks/_models.py`.
3. Add DB migration(s) and indexes for `claimed_by_session_id`.
4. Thread `claimed_by_session_id` through create, update, read, and manager paths:
   - `src/gobby/storage/tasks/_crud.py`
   - `src/gobby/storage/tasks/_manager.py`
5. Update lifecycle tools and ownership-related mutations to dual-write through the central transition layer instead of patching wrappers independently:
   - `claim_task`
   - `reopen_task`
   - `mark_task_needs_review`
   - `mark_task_review_approved`
   - `escalate_task`
   - `de_escalate_task`
6. Change session reconciliation logic to treat `claimed_by_session_id` as authoritative and `task_claimed` / `claimed_tasks` as derived mirrors.
7. Update session handoff, compaction, and spawn flows so ownership transfer updates `claimed_by_session_id` and derived mirrors consistently, while leaving `assignee` as compatibility-only state where still needed.
8. Add storage and MCP tests covering claim transfer, claim clearing, review ownership, and canonical-to-legacy projection for ownership-sensitive transitions.

### Acceptance criteria

- Ownership no longer has to be inferred from `status` or `assignee`.
- All major transition writers route through a shared implementation path.
- Session-level claim mirrors are consistent with stored task ownership.
- `assignee` is no longer required for active ownership semantics.

## Phase 2: Lifecycle/Blocked/Closed Split and Query Cutover

### Goal

Introduce canonical execution lifecycle and cut backend selection logic over to the new semantics in the same phase.

### Atomic tasks

1. Add `lifecycle_stage` to the task model and schema with values `null | in_progress | needs_review | review_approved`.
2. Stop treating `escalated` as a lifecycle stage in canonical storage; use escalation metadata as blocked state.
3. Make closed state canonical from close metadata rather than the legacy enum, while preserving the last `lifecycle_stage` under close.
4. Define and implement canonical derived predicates in task helpers:
   - `is_ready`
   - `is_blocked`
   - `is_closed`
   - `is_merge_ready`
5. Implement legacy compatibility mapping from canonical fields back to legacy `status` for interim callers.
6. Rewrite `list_tasks` filters to support canonical fields and derived predicates.
7. Rewrite `list_ready_tasks` to mean ready, unblocked work under canonical semantics rather than legacy `status='open'`.
8. Rewrite `list_blocked_tasks` to mean blocked by dependency or escalation predicates rather than ad hoc status checks.
9. Rewrite `suggest_next_task` scoring so it selects ready, unclaimed work and treats active claimed work separately from legacy `in_progress`.
10. Rewrite dependency-satisfaction logic so dependents unblock only when upstream tasks are closed.
11. Update full-text search, readiness helpers, and task aggregates to use canonical fields and predicates instead of raw legacy statuses.
12. Add tests proving:
   - ready/open projection
   - blocked/escalated projection
   - review-approved projection
   - closed projection
   - closed-only dependency satisfaction
   - suggestion behavior under canonical semantics

### Acceptance criteria

- Canonical task meaning is expressible without a single overloaded `status`.
- Backend task selection no longer depends on raw legacy status strings.
- Dependents unblock only from canonical close state.
- Legacy callers can continue to function temporarily through explicit projection.

## Phase 3: External Interfaces and Serialization

### Goal

Move external and serialized task interfaces to explicit state semantics while preserving temporary compatibility projections where needed.

### Atomic tasks

1. Update core task serialization so internal and external callers can access canonical fields plus projected legacy `status` during migration.
2. Update REST task endpoints to return canonical fields as primary state.
3. Update REST mutations so lifecycle transitions, ownership transitions, escalation, and close/reopen are separate operations instead of generic status mutation.
4. Update MCP task tools to prefer canonical fields and lifecycle-specific operations over generic status updates.
5. Redesign CLI filters around:
   - lifecycle stage
   - claimed/unclaimed
   - ready/blocked/closed
6. Update CLI rendering helpers to show lifecycle, ownership, blocked state, and closure separately.
7. Update CLI, admin health, and project stats aggregation to compute buckets from canonical predicates instead of enum counts.
8. Update JSONL sync and any other task export/import projections so canonical fields survive round-trip and projected legacy fields remain explicit compatibility output.
9. Add API, CLI, sync, and serialization tests for the new filter and output model.

### Acceptance criteria

- REST, MCP, CLI, admin, stats, and sync surfaces all expose the same primary state model.
- Generic status mutation is no longer the main way to drive task flow.
- Serialized task records preserve canonical state without forcing callers to infer meaning from the legacy enum.

## Phase 4: Rules, Agents, Pipelines, and Recovery

### Goal

Move automation and background recovery onto the new semantics.

### Atomic tasks

1. Update stop-gate rules to block on active claimed ownership rather than `in_progress` assumptions.
2. Update task-enforcement rules that mention review, reopen, de-escalation, and approval transitions to align with canonical lifecycle semantics.
3. Update agent definitions for developer, QA, expansion QA, nightly agents, and defaults so instructions and allowed tools reflect separate ownership and lifecycle semantics.
4. Update orchestrator pipeline task scans to route:
   - ready unclaimed developer work
   - claimed active work
   - `needs_review` unclaimed review work
   - merge-ready work
5. Update heartbeat, failure recovery, and any background monitors so they use canonical ownership and closure semantics instead of raw legacy status tests.
6. Update deprecated or dev pipelines still relied on in tests so they either migrate or are explicitly isolated from this epic.
7. Add workflow and pipeline test coverage for the new routing and recovery semantics.

### Acceptance criteria

- No pipeline or rule dispatches work based on stale status assumptions.
- Review ownership, merge-ready semantics, and claimed-work enforcement are consistent.
- Recovery paths preserve canonical ownership and lifecycle meaning.

## Phase 5: Web UI

### Goal

Move the web UI from status-centric presentation to explicit task-state presentation.

### Atomic tasks

1. Update task API hooks and TypeScript task types to include canonical state fields.
2. Replace single-status badge and render logic with derived display labels composed from lifecycle, blocked state, claim ownership, and closure.
3. Update task detail actions so ownership changes, lifecycle changes, escalation, and close/reopen are distinct UI actions.
4. Redesign Kanban columns around canonical concepts:
   - Ready
   - In Progress
   - Review
   - Blocked
   - Merge Ready
   - Closed
5. Update dashboard, activity summaries, and digest views to aggregate from canonical predicates.
6. Update tests for hooks and task components to reflect the new state model.
7. Audit charts or timelines that currently rely on status ordering and replace them with explicit derived ordering.

### Acceptance criteria

- The web UI does not require the legacy `status` enum as its primary presentation model.
- UI actions map cleanly to ownership, lifecycle, blocked state, and close/reopen transitions.

## Phase 6: Future Workflow-Phase Follow-up

### Goal

Keep planning and expansion workflows extensible without polluting execution lifecycle.

### Atomic tasks

1. Document that planning and expansion states belong on a separate workflow axis, not on execution lifecycle.
2. Keep execution lifecycle reserved for build, review, approval, and close semantics.
3. Evaluate whether the existing `expansion_status` axis is sufficient or whether a future `workflow_phase` field is justified.
4. Create a follow-up epic for planning and expansion state integration if additional workflow-phase work is needed.

### Acceptance criteria

- Future states like `needs_planning` or `needs_expansion` can be added later without reopening the execution lifecycle split.
- This epic lands without re-entangling execution and planning semantics.

## Risks and Watchpoints

- The highest migration risk is semantic drift between duplicated readers and writers during the transition. Centralizing transitions early is required, not optional.
- The "closed only unblocks dependents" decision changes scheduling behavior, not just storage shape. Query cutover and canonical field introduction must land together.
- `assignee` staying temporarily is acceptable, but it must be clearly documented and enforced as non-canonical ownership state.
- Preserving lifecycle under close improves auditability, but reopen behavior must be implemented deliberately so callers do not accidentally restore tasks into the wrong active state.

## Assumptions

- Phase 0 is a stabilizer and must not become the final architecture.
- Backward compatibility is not the priority, but repo operability during the refactor is.
- The epic should be decomposed into child tasks by phase, with Phase 0 and Phase 1 prioritized first.
