## Revision 1 — 2026-04-21

This is a revision of `task-12079-plan.md`. The original is preserved unchanged as the audit trail for the task tree expanded from it.

**Why revised:** The original plan scoped a new first-class task status `changes_requested` with verb `mark_task_changes_requested` (Option A). Blast-radius audit measured ~60 files actually affected (28 UI components, 19 YAML workflow/rule files, 27 tests, ~20 backend lifecycle surfaces) — the original "~8 files" estimate was wildly optimistic.

**Revised design (Option C.2):** No new status. New verb `mark_task_review_rejected(rejection_notes: str, round: int | None = None)` transitions `needs_review → open` (existing status), appends findings to description, bumps round label. Autonomous conductor's existing `status == "open"` re-dispatch gate handles the round-trip with zero new branches — the prefix-parsing just gets deleted, no replacement. Total cost: ~14 files (including ~7 one-line blocklist additions for permissions hygiene).

**Original Option A summary** (for audit): see `task-12079-plan.md` sections "Phase 1: Persistence + state semantics" (status enum + 3 whitelists + new `lifecycle_stage=None` handling) and "Phase 3.2" (new conductor branch on `changes_requested`). Both obsolete under C.2.

**Downstream task tree:** the tree expanded from the original still exists but several tasks need description/scope rewrites or deletion:
- #12164 (enum addition) → DELETED (no enum change under C.2)
- #12174 (new conductor branch) → scope rewritten to deletion of prefix parsing
- All verb-reference tasks → rename `mark_task_changes_requested` to `mark_task_review_rejected`
- New permissions-hygiene task added under #12161

Subsequent revisions, if any, should follow the same copy-and-version convention (`task-12079-plan-rev2.md`, etc.) — never edit historical plan docs in place.

---

# Introduce `mark_task_review_rejected` verb (no status enum change)

## Overview

Today's plan-adversary agent uses `escalate_task` with reason prefix `planning_changes_requested:` for routine revision rounds. The autonomous conductor branches on `status == "escalated"` + reason-prefix parsing (`src/gobby/mcp_proxy/tools/tasks/_front_half.py:133`), which conflates routine "reviewer wants changes" with true "human intervention required" — both land on `escalated`. Parsing verdict meaning out of a prose reason string is brittle and has no symmetric verb to `mark_task_review_approved`.

Fix is a new transition verb `mark_task_review_rejected(task_id, rejection_notes, round=None)` that transitions `needs_review → open`, appends a structured `## Adversary Findings — Round N` entry to the task description, and optionally bumps the `planning-round:N` label. The conductor's existing `status == "open"` re-dispatch gate handles the round-trip — prefix parsing is deleted with no replacement branch. No new status. No enum cascade. No UI changes.

The verdict now has a machine-readable carrier (the verb name), symmetric with `mark_task_review_approved`. The three reviewer verdicts are:

| Outcome | Verb | Lifecycle effect |
|---|---|---|
| Approved | `mark_task_review_approved(approval_notes=...)` | `needs_review → review_approved` |
| Blocking findings, revision needed | `mark_task_review_rejected(rejection_notes=..., round=N)` | `needs_review → open`; conductor re-dispatches planner |
| True human intervention | `escalate_task(reason="needs_requirements: …"|"blocked: …")` | `* → escalated`; human unblock required |

## Constraints

- **No status enum change.** `mark_task_review_rejected` transitions to the existing `open` status. The `open` status becomes polymorphic in the sense that it can be entered from `reopen_task` (from `closed`), `de_escalate_task` (from `escalated`), fresh creation, and now `mark_task_review_rejected` (from `needs_review`) — this is already the case pre-migration; we add one more entry path.
- **Ownership.** Task is released (unclaimed) on `mark_task_review_rejected`, matching `mark_task_needs_review`'s `release_task_claim` pattern at `src/gobby/storage/tasks/_transitions.py:63, 189`. Plan-adversary auto-claims via `spawn_agent`; the claim is released as part of the verdict.
- **No new timestamp column.** Follow the `mark_task_needs_review` precedent — no `review_rejected_at`.
- **No reason-prefix parsing anywhere.** Conductor routing uses status (`open` for re-dispatch, `escalated` for halt), never description parsing.
- **Do NOT add tests to monolith files.** `tests/workflows/test_rule_engine.py` (2,695 LOC), `tests/workflows/test_hooks.py` (1,569 LOC), `tests/workflows/test_stop_gates_rules.py` (1,383 LOC) — route new coverage into new focused files.
- **Existing tests depend on current contracts.** `tests/skills/test_plan_review_skill.py:109` and `tests/agents/test_plan_adversary_loads_plan_review.py:87` will need updates to reflect the new verb; do not skip them.

## Phase 1: Persistence + state semantics

### 1.1 Add `mark_task_review_rejected` transition handler [category: code]

Targets:
- `src/gobby/storage/tasks/_transitions.py` — new `mark_task_review_rejected(task_id: str, rejection_notes: str, round: int | None = None) -> Task` function near `mark_task_needs_review` at line 189.

Behavior: source status must be `needs_review`; target status `open`; calls `release_task_claim` if claim still held (same pattern as `mark_task_needs_review`); appends structured `## Adversary Findings — Round N` entry to description (existing convention at `plan-review/SKILL.md:158-207`); optionally bumps `planning-round:N` label if `round` supplied. Raises `ValueError` if called from any status other than `needs_review`.

Verification: `tests/mcp_proxy/tools/tasks/test_review_rejected_transition.py`:
- Happy path: task in `needs_review` → `mark_task_review_rejected(..., rejection_notes="blocking: X")` → status becomes `open`, description has new findings heading, task unclaimed.
- Precondition: same call from `open` or `in_progress` raises `ValueError`.
- Round preservation: call with `round=2` bumps label; prior `## Adversary Findings — Round 1` entries remain in description.

**No enum or whitelist changes required.** `open` is already in the status enum, already in `LIFECYCLE_STAGES` (implicitly via the open→in_progress→needs_review pipeline), and the conductor already re-dispatches on it.

## Phase 2: MCP and HTTP surfaces

### 2.1 Register `mark_task_review_rejected` MCP verb [category: code]

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py` — new registration alongside `mark_task_review_approved`. Use `mark_task_review_approved` as shape reference for auth/permission boilerplate (both verbs are reviewer-emitted verdicts).

Verification: schema-discovery test confirms the new verb appears in `gobby-tasks` tool list and the inputSchema has required fields (`task_id`, `rejection_notes`).

### 2.2 Add HTTP route + broadcast [category: code]

Targets:
- `src/gobby/servers/routes/tasks.py:95-98` — new `TaskReviewRejectedRequest` schema (mirrors `TaskReviewRequest`'s structure with a `rejection_notes` field and optional `round`).
- `src/gobby/servers/routes/tasks.py:~455` (after `review-approved` at line 433) — new `@router.post("/{task_id}/review-rejected")` handler. Calls `server.task_manager.mark_task_review_rejected(...)`. Broadcasts `task_review_rejected` event via `_broadcast_task`.

No `TaskReleaseClaimRequest` or `TaskDeEscalateRequest` changes — the verb transitions to existing `open`, which those schemas already handle.

Verification: `tests/servers/routes/test_tasks_review_rejected.py`:
- `POST /{task_id}/review-rejected` returns 200 with updated task payload; broadcast event fires; task status becomes `open`.

## Phase 3: Agent + orchestrator

### 3.1 Update plan-adversary agent contract [category: config]

Targets:
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml` — `review` step (~line 116):
  - `on_mcp_success` transition triggers: add `mark_task_review_rejected` alongside `mark_task_review_approved` and `escalate_task`.
  - No allowlist change needed — the `review` step uses `allowed_tools: "all"` + `blocked_mcp_tools`; the new verb is not in the blocklist, so it's automatically allowed.
  - Remove `planning_changes_requested:` reason-prefix guidance from the `instructions` block.
  - CRITICAL RULES section (lines 47-52): no change needed; `escalate_task` remains allowed for true human intervention.
  - `instructions` block (lines 22-52): inline the new verdict matrix (approved → `mark_task_review_approved`; blocking findings → `mark_task_review_rejected`; true human intervention → `escalate_task` with `needs_requirements:` or `blocked:` prefix).

Verification: `tests/agents/test_plan_adversary_loads_plan_review.py:87` updated to assert the new verb triggers the `on_mcp_success` transition.

### 3.2 Delete `PLANNING_CHANGES_REQUESTED_PREFIX` parsing from conductor [category: code]

Targets:
- `src/gobby/mcp_proxy/tools/tasks/_front_half.py:133-161` — remove the `PLANNING_CHANGES_REQUESTED_PREFIX` escalation-reason-parsing branch entirely, and the branch's `de_escalate_task(target_status="open")` call. `status == "escalated"` branch now only handles `needs_requirements:` / `blocked:` reasons — both trigger the user-intervention halt.
- `_front_half.py:202` — existing `if planning_task.status == "open":` gate handles `mark_task_review_rejected`'s output; no change needed.
- `_front_half.py:871-908` (`_planner_prompt`) — when dispatching on `open` with a `## Adversary Findings — Round N` section present, surface that findings section in the prompt.
- Grep and remove all other usage sites of `PLANNING_CHANGES_REQUESTED_PREFIX` across `src/` and `tests/`.
- `src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml` — verify no step condition checks escalation reason prefix; if any exists, update to check status.

Verification: existing `_front_half.py` tests updated to exercise the new re-dispatch-on-`open`-with-findings branch. Manual E2E: spawn plan-adversary on a task with a deliberately incomplete plan → verdict lands on `open` with findings (not `escalated`); conductor re-dispatches planner automatically via the existing `open` gate.

### 3.3 Permissions hygiene: block `mark_task_review_rejected` in non-reviewer agents [category: config]

Mirror the existing `mark_task_review_approved` block pattern. Add `- "gobby-tasks:mark_task_review_rejected"` to the `blocked_mcp_tools` list in:
- `src/gobby/install/shared/workflows/agents/default.yaml`
- `src/gobby/install/shared/workflows/agents/developer.yaml`
- `src/gobby/install/shared/workflows/agents/requirements-analyst.yaml`
- `src/gobby/install/shared/workflows/agents/qa-dev.yaml`
- `src/gobby/install/shared/workflows/agents/python-dev.yaml`
- `src/gobby/install/shared/workflows/agents/planner.yaml`
- `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`

Plan-adversary.yaml does NOT block the verb — it's the sole reviewer agent.

Verification: grep each agent YAML for the new verb in `blocked_mcp_tools:` context.

## Phase 4: Docs

### 4.1 Rewrite plan-review skill escalation policy [category: docs]

Targets:
- `src/gobby/install/shared/skills/plan-review/SKILL.md:132-149` — replace current Escalation Policy with explicit verdict matrix:

| Outcome | Verb | Effect |
|---|---|---|
| Plan is sound | `mark_task_review_approved(approval_notes=...)` | → `review_approved` |
| Blocking findings, revision needed | `mark_task_review_rejected(rejection_notes=..., round=N)` | → `open`; conductor re-dispatches planner on the existing `open` gate |
| True human intervention needed | `escalate_task(reason="needs_requirements: ...")` or `escalate_task(reason="blocked: ...")` | → `escalated`, human unblock required |

### 4.2 Update /gobby plan skill Step 7.6 branches [category: docs]

Targets:
- `src/gobby/install/shared/skills/plan/SKILL.md:303-321` — rewrite branches to key off status + findings content:
  - `review_approved` → Step 8.
  - `open` AND description contains `## Adversary Findings — Round {current_round + 1}` → revision round (read findings, present to user, bump `current_round` from label, budget check, re-enter plan mode, loop to 7.4).
  - `escalated` → true halt, Step 9 (surface `escalation_reason`; no auto-clarification loop).
  - Any other state (`in_progress`, `open` without findings, `needs_review`, `closed`) → treat as adversary crash; go to Step 9.
- Delete `de_escalate_task` calls at old lines 312/318 (task is already in `open` after rejection — no de-escalation needed).
- Preserve lines 323–325 ("Why re-enter plan mode each round") verbatim.

Verification: `tests/skills/test_plan_review_skill.py:109` updated with the new matrix; no stale `planning_changes_requested:` expectations remain anywhere in tests.

## Overall verification checklist

- [ ] `uv run pytest tests/mcp_proxy/tools/tasks/test_review_rejected_transition.py tests/servers/routes/test_tasks_review_rejected.py -v`
- [ ] `uv run pytest tests/skills/test_plan_review_skill.py tests/agents/test_plan_adversary_loads_plan_review.py -v`
- [ ] Grep across `src/` for `PLANNING_CHANGES_REQUESTED_PREFIX` returns no hits.
- [ ] Grep across `src/` and `tests/` for the string `planning_changes_requested:` returns no hits (constant removed).
- [ ] Grep across `src/` for `mark_task_changes_requested` returns no hits (never implemented under C.2).
- [ ] Grep across `src/` for `changes_requested` status returns no hits (never added under C.2).
- [ ] Manual E2E: plan-adversary on a bad plan lands the task on `open` with findings; front-half re-dispatches planner automatically.
- [ ] HTTP E2E: `curl -X POST localhost:60887/api/tasks/{id}/review-rejected -d '{"rejection_notes":"..."}'` returns 200, broadcast fires.
- [ ] `uv run ruff check src/ && uv run mypy src/` clean.

## Reference

Parent campaign plan: `~/.claude/plans/handoff-interactive-planning-for-twinkly-widget.md` Task 4.

Original (Option A) plan preserved at: `.gobby/plans/task-12079-plan.md`.
