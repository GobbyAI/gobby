# Add first-class `changes_requested` verdict (contract migration)

## Overview

Today's plan-adversary agent uses `escalate_task` with reason prefix `planning_changes_requested:` for routine revision rounds. The autonomous conductor branches on `status == "escalated"` + reason-prefix parsing (`src/gobby/mcp_proxy/tools/tasks/_front_half.py:133`), which conflates routine "reviewer wants changes" with true "human intervention required" — both land on `escalated`. Parsing verdict meaning out of a prose reason string is brittle and has no symmetric verb to `mark_task_review_approved`.

Fix is a contract migration that introduces a new first-class task status `changes_requested` with a dedicated transition verb `mark_task_changes_requested(task_id, notes, round=None)`. The verdict now has a machine-readable carrier (status), not a prose prefix. The migration touches five parallel surfaces: task persistence, MCP verb, HTTP routes, agent contract, autonomous orchestrator. Docs follow from the contract.

## Constraints

- **Lifecycle stage.** `changes_requested` is a **legacy-only status** with `lifecycle_stage = None`. Do NOT add it to `LIFECYCLE_STAGES` at `src/gobby/tasks/state_semantics.py:23-27`. Symmetric with `open`, `closed`, `escalated`.
- **Ownership.** Task is **released (unclaimed)** on `mark_task_changes_requested`, matching `mark_task_needs_review`'s `release_task_claim` pattern at `src/gobby/storage/tasks/_transitions.py:63, 189`. Do NOT add `changes_requested` to `ACTIVE_CLAIM_STATUSES` at `src/gobby/tasks/state_semantics.py:29-35`. Do NOT add to `DE_ESCALATION_TARGET_STATUSES` at lines 37-42 — not reached via de-escalation.
- **No new timestamp column.** Current pattern at `src/gobby/storage/tasks/_models.py:140` and `_crud.py:414` only timestamps `closed_at` and `escalated_at`. There is no `needs_review_at` or `review_approved_at`. Do NOT add `changes_requested_at`; follow the `needs_review` precedent.
- **No reason-prefix parsing anywhere.** Orchestrator routing uses `status`, not description parsing.
- **Do NOT add tests to monolith files.** `tests/workflows/test_rule_engine.py` (2,695 LOC), `tests/workflows/test_hooks.py` (1,569 LOC), `tests/workflows/test_stop_gates_rules.py` (1,383 LOC) — route new coverage into new focused files.
- **Existing tests depend on current contracts.** `tests/skills/test_plan_review_skill.py:109` and `tests/agents/test_plan_adversary_loads_plan_review.py:87` will need updates to reflect the new verb; do not skip them.

## Phase 1: Persistence + state semantics

### 1.1 Add `changes_requested` to status enum and legacy-status whitelist [category: code]

Targets:
- `src/gobby/storage/tasks/_models.py:116-127` — add `"changes_requested"` to status Literal.
- `src/gobby/tasks/state_semantics.py:14-21` — add to `LegacyTaskStatus`.
- `src/gobby/storage/tasks/_crud.py:32-39` — add to `_LEGACY_TASK_STATUSES` whitelist.
- `src/gobby/storage/tasks/_crud.py:407-434` — verify `if normalized_status in {"open", "in_progress", "needs_review", "review_approved"}:` at line 411 still excludes `changes_requested` (projects to `lifecycle_stage = None`). Add an explicit comment documenting this.

Verification: `tests/storage/tasks/test_changes_requested_projection.py`:
- Insert a task with `status="changes_requested"`; assert `lifecycle_stage IS NULL`.
- Assert `"changes_requested" in _LEGACY_TASK_STATUSES`.
- Assert `"changes_requested" NOT in ACTIVE_CLAIM_STATUSES`.
- Assert `"changes_requested" NOT in LIFECYCLE_STAGES`.
- Assert `"changes_requested" NOT in DE_ESCALATION_TARGET_STATUSES`.

### 1.2 Add `mark_task_changes_requested` transition handler [category: code]

Targets:
- `src/gobby/storage/tasks/_transitions.py` — new `mark_task_changes_requested(task_id: str, notes: str, round: int | None = None) -> Task` function near `mark_task_needs_review` at line 189.

Behavior: source status must be `needs_review`; target status `changes_requested`; calls `release_task_claim` (same as `mark_task_needs_review`); appends structured `## Adversary Findings — Round N` entry to description (existing convention at `SKILL.md:158-207`); optionally bumps `planning-round:N` label if `round` supplied. Raises `ValueError` if called from any status other than `needs_review`.

Verification: `tests/mcp_proxy/tools/tasks/test_changes_requested_transition.py`:
- Happy path: task in `needs_review` → `mark_task_changes_requested(..., notes="blocking: X")` → status becomes `changes_requested`, description has new findings heading, task unclaimed.
- Precondition: same call from `open` or `in_progress` raises `ValueError`.
- Round preservation: call with `round=2` bumps label; prior `## Adversary Findings — Round 1` entries remain in description.

## Phase 2: MCP and HTTP surfaces

### 2.1 Register `mark_task_changes_requested` MCP verb [category: code]

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py` — new registration alongside `mark_task_review_approved`. Use `de_escalate_task` at lines 461-476 as shape reference for auth/permission boilerplate.

Verification: schema-discovery test confirms the new verb appears in `gobby-tasks` tool list and the inputSchema has required fields (`task_id`, `notes`).

### 2.2 Add HTTP route + broadcast [category: code]

Targets:
- `src/gobby/servers/routes/tasks.py:95-98` — new `TaskChangesRequestedRequest` schema (mirrors `TaskReviewRequest`'s `notes` field).
- `src/gobby/servers/routes/tasks.py:~455` (after `review-approved` at line 433) — new `@router.post("/{task_id}/changes-requested")` handler. Calls `server.task_manager.mark_task_changes_requested(...)`. Broadcasts `task_changes_requested` event via `_broadcast_task`.
- `src/gobby/servers/routes/tasks.py:87-92` — add `"changes_requested"` to `TaskReleaseClaimRequest.status` Literal (claim-release path accepts the new projected status during ownership release).
- `src/gobby/servers/routes/tasks.py:128` — do NOT add `changes_requested` to `TaskDeEscalateRequest.target_status` (not a de-escalation target).

Verification: `tests/servers/routes/test_tasks_changes_requested.py`:
- `POST /{task_id}/changes-requested` returns 200 with updated task payload; broadcast event fires; task status becomes `changes_requested`.
- `TaskReleaseClaimRequest(status="changes_requested")` deserializes cleanly.
- `TaskDeEscalateRequest(target_status="changes_requested")` fails validation.

## Phase 3: Agent + orchestrator

### 3.1 Update plan-adversary agent contract [category: config]

Targets:
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml` — `review` step (~line 116):
  - Add `mark_task_changes_requested` to `allowed_mcp_tools`.
  - Add to `on_mcp_success` transition triggers (alongside `mark_task_review_approved` and `escalate_task`).
  - Remove `planning_changes_requested:` reason-prefix guidance.
  - CRITICAL RULES section (lines 47-52): update banned-verb list to reflect the new verb as permitted.
  - `instructions` block (lines 22-52): inline the new verdict matrix (approved → `mark_task_review_approved`; blocking findings → `mark_task_changes_requested`; true human intervention → `escalate_task` with `needs_requirements:` or `blocked:` prefix).

Verification: `tests/agents/test_plan_adversary_loads_plan_review.py:87` updated to assert the new verb is in the `review` step's allowed list and the transition fires on it.

### 3.2 Extend autonomous conductor to dispatch planner on `changes_requested` [category: code]

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_front_half.py:202` — extend gate: `if planning_task.status in ("open", "in_progress", "changes_requested"):`. Primary change; existing claimed-vs-unclaimed branches handle `changes_requested` correctly because the transition releases the claim.
- `_front_half.py:133-161` — remove `PLANNING_CHANGES_REQUESTED_PREFIX` escalation-reason-parsing branch. `status == "escalated"` branch now only handles `needs_requirements:` / `blocked:` reasons.
- `_front_half.py:871-908` (`_planner_prompt`) — when dispatching on `changes_requested`, surface the most recent `## Adversary Findings — Round N` from the task description in the prompt.
- Grep and remove all other usage sites of `PLANNING_CHANGES_REQUESTED_PREFIX`.
- `src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml` — if any step conditions check escalation reason prefix, update to check status.

Verification: existing `_front_half.py` tests updated to exercise the new `changes_requested` branch. Manual E2E: spawn plan-adversary on a task with a deliberately incomplete plan → verdict lands on `changes_requested` (not `escalated`); conductor re-dispatches planner automatically.

## Phase 4: Docs

### 4.1 Rewrite plan-review skill escalation policy [category: docs]

Targets:
- `src/gobby/install/shared/skills/plan-review/SKILL.md:132-149` — replace current Escalation Policy with explicit verdict matrix:

| Outcome | Verb | Effect |
|---|---|---|
| Plan is sound | `mark_task_review_approved` | → `review_approved` |
| Blocking findings, revision needed | `mark_task_changes_requested(notes=...)` | → `changes_requested`, conductor re-dispatches planner |
| True human intervention needed | `escalate_task(reason="needs_requirements: ...")` or `escalate_task(reason="blocked: ...")` | → `escalated`, human unblock required |

### 4.2 Update /gobby plan skill Step 7.6 branches [category: docs]

Targets:
- `src/gobby/install/shared/skills/plan/SKILL.md:301-322` — rewrite branches to key off status:
  - `review_approved` → Step 8.
  - `changes_requested` → revision round (read latest `## Adversary Findings — Round N` from description, revise, loop).
  - `escalated` → true halt, Step 9.

Verification: `tests/skills/test_plan_review_skill.py:109` updated with the new matrix; no stale `planning_changes_requested:` expectations remain anywhere in tests.

## Overall verification checklist

- [ ] `uv run pytest tests/storage/tasks/test_changes_requested_projection.py tests/mcp_proxy/tools/tasks/test_changes_requested_transition.py tests/servers/routes/test_tasks_changes_requested.py -v`
- [ ] `uv run pytest tests/skills/test_plan_review_skill.py tests/agents/test_plan_adversary_loads_plan_review.py -v`
- [ ] Grep across `src/` for `PLANNING_CHANGES_REQUESTED_PREFIX` returns no hits.
- [ ] Grep across `src/` and `tests/` for the string `planning_changes_requested:` returns no hits (constant removed).
- [ ] Manual E2E: plan-adversary on a bad plan lands `changes_requested`; front-half re-dispatches planner.
- [ ] HTTP E2E: `curl -X POST localhost:60887/api/tasks/{id}/changes-requested -d '{"notes":"..."}'` returns 200, broadcast fires.
- [ ] `uv run ruff check src/ && uv run mypy src/` clean.

## Reference

Parent campaign plan: `~/.claude/plans/handoff-interactive-planning-for-twinkly-widget.md` Task 4.
