# Delegated-autonomy branch in /gobby plan + autonomous planner edit-scope (unified rule)

## Overview

Two related gaps solved together via one unified edit-scope rule and minimal skill/pipeline edits.

**Path A — Interactive delegated-autonomy.** `/gobby plan` Step 7.6 forces `EnterPlanMode`/`ExitPlanMode` every revision round, popping a UI approval prompt per round (canonical approval gate at `src/gobby/servers/chat_session_permissions.py:117` + `src/gobby/servers/websocket/handlers/plan_approval.py:139`, matching `tests/servers/test_chat_session_permissions.py:57`). Delegated-autonomy users want "wake me at terminal state" semantics. Replace the existing A/P menu at `src/gobby/install/shared/skills/plan/SKILL.md:52-71` with a three-option I/D/P menu.

**Path B — Autonomous front-half planner.** The `planner` agent (`src/gobby/install/shared/workflows/agents/planner.yaml`) has `allowed_tools: "all"` on its `plan` step with no constraint on where it writes. Gate edits to the current plan artifact only.

Both paths share one rule: "a planning session is in progress, restrict writes to the current plan artifact, lift when the task reaches a terminal status."

## Constraints

- **Canonical approval model is gobby-side, not provider-native.** Task 5 must NOT depend on Claude Code provider hooks firing for `ExitPlanMode` or other plan-mode transitions. Anthropic docs do not surface explicit guarantees about `ExitPlanMode` hook coverage in plan mode. Path A runs entirely through `chat_session_permissions.py:117` and `plan_approval.py:139`.
- **Canonical artifact variable: `artifact_path`.** Already used by existing skill at `SKILL.md:172, 205, 215, 234, 280, 323, 336, 390` and by `_front_half.py:516` and `expand-task.yaml:21`. **Repo-relative**, e.g. `.gobby/plans/task-12044-plan.md`. Do NOT introduce a parallel `plan_artifact_path` variable.
- **Step-enforcement cannot carve per-path exceptions.** `src/gobby/workflows/engine/enforcement.py:147, 159` matches only on tool names. If the unified rule fails to fire for planner sessions, the fix is to identify and correct the dependency (helper wiring, spawn-time variables) — NOT to add a step-level `blocked_tools` allowance or a second rule.
- **Mode value mapping, explicit.** Three values for `plan_review_mode`: `"adversarial"` (interactive), `"delegated"` (new), `"plain"` (existing). There is NOT a fourth `"interactive"` value — the user-facing menu label "Interactive" maps to the existing `"adversarial"` string. Codify in the skill menu.
- **Dependency: this task lands after #12044 implementation.** `task_status_in` helper is a new condition helper that requires `task_manager` threaded through `build_condition_helpers(task_manager=...)`. That wiring is what #12044 delivers (`src/gobby/workflows/safe_evaluator.py:336-341` via `src/gobby/workflows/engine/templating.py:108`). Without it, `task_status_in` stubs to `False` and Path B silently breaks.
- **Dependency: #12079 lands first.** `task_status_in` uses `"changes_requested"` and the skill branches reference the new status.
- **Stale test follow-up deferred.** `tests/servers/test_chat_session_permissions_plan.py` documents a divergent model. Not authoritative for this task. Cleanup handled by #12129.

## Phase 1: Helpers

### 1.1 Add `is_current_plan_artifact` exact-path helper [category: code]

Problem: existing `is_plan_file` at `src/gobby/workflows/enforcement/blocking.py:143-148` whitelists any `.md` under `.gobby/`, `.claude/`, `.gemini/`, `.codex/` — too permissive for "only the plan artifact."

Targets:
- `src/gobby/workflows/enforcement/blocking.py` — add alongside `is_plan_file`:
  ```python
  def is_current_plan_artifact(
      file_path: str,
      artifact_path: str | None,
      project_path: str | None = None,
  ) -> bool:
      """True iff file_path is the canonical plan artifact.

      artifact_path is repo-relative (e.g. '.gobby/plans/task-12044-plan.md').
      file_path comes in absolute form from Edit/Write; normalize to repo-relative
      using project_path, resolve '..' segments, strip trailing slashes, then
      compare. False when artifact_path is None/empty or paths don't match exactly.
      """
      ...
  ```

Verification: `tests/workflows/test_is_current_plan_artifact_helper.py`:
- Exact match (absolute file_path for the repo-relative artifact): True.
- Neighboring `.md` in `.gobby/plans/`: False (i.e., NOT whitelisted as `is_plan_file` would be).
- `artifact_path=None` or empty: False.
- Path normalization: trailing slashes, `..` segments, absolute-vs-relative consistency all normalize correctly.

### 1.2 Register helper with `project_path` closure [category: code]

Targets:
- `src/gobby/workflows/engine/templating.py:105-119` — in `_build_allowed_funcs`, alongside `funcs["is_plan_file"] = is_plan_file`:
  ```python
  project_path = (ctx.get("project") or {}).get("path")
  funcs["is_current_plan_artifact"] = (
      lambda file_path, artifact_path: is_current_plan_artifact(
          file_path, artifact_path, project_path=project_path
      )
  )
  ```
  Eval context exposes `project.path` at `templating.py:72`. Closure pattern matches `is_server_listed` / `is_tool_unlocked` at lines 110-111.

### 1.3 Add `task_status_in` condition helper [category: code]

Targets:
- `src/gobby/workflows/condition_helpers.py` — add alongside `task_tree_complete`, `task_needs_human_review`, `task_has_label_prefix`:
  ```python
  def task_status_in(task_manager, task_id: str, *statuses: str) -> bool:
      """True iff the task's current status is in the given set.

      Stubs to False when task_manager is None (matches existing three helpers).
      """
      ...
  ```
- `src/gobby/workflows/safe_evaluator.py:336-341` — `build_condition_helpers` already accepts `task_manager`; register `task_status_in` in the returned dict.

Verification: `tests/workflows/test_condition_helpers_task_status.py`:
- Returns correct boolean per task status.
- Stubs to `False` when `task_manager is None`.
- Integration test asserting that after #12044's wiring lands, `RuleEngine.evaluate` sees non-stub behavior. Mark as an integration gate — failing means #12044 wiring regressed.

## Phase 2: Unified edit-scope rule

### 2.1 New rule YAML [category: config]

Targets:
- New file `src/gobby/install/shared/workflows/rules/plan-mode/block-writes-outside-plan-artifact.yaml`:

```yaml
tags: [plan-mode, enforcement, gobby, default]

rules:
  block-writes-outside-plan-artifact:
    description: "Block file edits outside the current plan artifact during an active planning session"
    event: before_tool
    enabled: true
    priority: 21
    when: >
      (
        (
          variables.get('_agent_type') == 'planner'
          and not task_status_in(
            variables.get('assigned_task_id'),
            'review_approved', 'escalated', 'closed'
          )
        )
        or (
          variables.get('plan_review_mode') == 'delegated'
          and variables.get('interactive_lock_label')
        )
      )
      and not is_current_plan_artifact(
        tool_input.get('file_path', ''),
        variables.get('artifact_path')
      )
    effects:
      - type: block
        tools: [Edit, Write, NotebookEdit]
        reason: |
          A planning session is in progress — only the current plan artifact
          (variables.artifact_path) may be edited. If artifact_path is not set,
          ALL edits are blocked (skill-level precondition should have prevented
          reaching this state). The gate lifts when the planning task reaches
          review_approved, escalated, or closed (autonomous) or when the
          delegated /gobby plan flow exits (interactive).
```

Priority 21 slots just above existing `block-edits-plan-mode.yaml` (priority 20). Two rules are orthogonal (old gates on `plan_mode == true`; new gates on planning-session identity).

Verification: `tests/workflows/test_plan_artifact_edit_scope_rule.py`:
- Path A fires: `plan_review_mode="delegated"` + `interactive_lock_label` + `artifact_path` set → Write on non-artifact path blocked; Write on artifact path passes.
- Path B fires: `_agent_type="planner"` + `assigned_task_id` referencing a non-terminal task → Write on non-artifact path blocked.
- Path B lifts at terminal: `review_approved` / `escalated` / `closed` → rule does not fire.
- Normal user session (neither branch's vars set) → rule does not fire.
- **Fail-closed test**: delegated+locked but `artifact_path` absent → rule FIRES (outer gate holds; inner `is_current_plan_artifact(file, None)` returns False; all writes blocked). Skill precondition prevents reaching this state in practice; test codifies the defensive behavior.

## Phase 3: Skill changes

### 3.1 Replace Step 1a A/P menu with three-option I/D/P menu [category: docs]

Targets:
- `src/gobby/install/shared/skills/plan/SKILL.md:52-71` — replace existing A/P menu with:

```
How would you like to review this plan?
  I) Interactive — spawn plan-adversary each round, per-round approval via ExitPlanMode
     (recommended; bundled in this skill)
  D) Delegated — spawn plan-adversary each round, no per-round approval; wake you only at
     terminal state (approval, escalation, or round budget exhausted)
  P) Plain — draft, approve, hand off to /gobby expand manually
Choice [I]:
```

Both I and D ask for `max_rounds` (default 3). P retains existing behavior unchanged.

Explicit mode-value mapping (persist using existing `set_variable` pattern):
- `I` → `plan_review_mode = "adversarial"` (existing string — preserves backwards compat with persisted state at `SKILL.md:67` and existing Step 7.6 branches).
- `D` → `plan_review_mode = "delegated"` (new).
- `P` → `plan_review_mode = "plain"` (existing).

Power-user override: skill checks pre-set `plan_review_mode` session var and skips the menu if already set to a valid value.

### 3.2 Step 7.6 delegated branch [category: docs]

Targets:
- `src/gobby/install/shared/skills/plan/SKILL.md:301-322`:
  - `plan_review_mode == "adversarial"` → existing per-round `EnterPlanMode`/`ExitPlanMode` loop, unchanged.
  - `plan_review_mode == "delegated"` → skip `EnterPlanMode`/`ExitPlanMode` on rounds 2+; edit plan file in place; re-spawn plan-adversary. Only terminal states interrupt: `review_approved` → Step 8; `changes_requested` (from #12079) with round budget exhausted → Step 9; `escalated` → Step 9; adversary crash → Step 9.
  - `plan_review_mode == "plain"` → unchanged.

### 3.3 Add Step 7 entry precondition [category: docs]

Targets:
- `src/gobby/install/shared/skills/plan/SKILL.md` — before entering Step 7 for any mode other than `"plain"`: gate on `artifact_path` session variable being set. Skill already computes and persists it at Step 4 (`SKILL.md:215`); plain-mode handoff already depends on this at `SKILL.md:234`. Add the gate check at Step 7 entry: if the variable is absent, abort with a clear message rather than silently proceeding into a fail-closed state.

Verification: `tests/skills/test_plan_skill_delegated_mode.py`:
- Menu selection: I / D / P all set the correct `plan_review_mode` value.
- Pre-set session var: skill skips the menu and honors the existing value.
- Step 7 entry with `artifact_path` absent: skill aborts with a clear message (simulate by clearing the variable mid-flow).
- Terminal-state interruption: `review_approved` and `escalated` break the loop; `changes_requested` continues until round budget exhausted.

## Phase 4: Planner spawn-time plumbing

### 4.1 Pipeline-level `initial_variables.artifact_path` injection [category: config]

Problem: `_front_half.py:537` returns a dispatch payload (`agent, task_id, task_ref, prompt`) — not an actual spawn. The real spawn happens at `front-half-orchestrator.yaml:79-90` via `spawn_agent`. Currently the planner session has no `artifact_path` session variable, so the unified rule's Path B evaluates `is_current_plan_artifact(file, None)` → False and blocks ALL writes including to the plan file itself. Universal deny.

`gobby-agents:spawn_agent` already accepts `initial_variables: dict[str, Any] | None = None` at `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py:68`, merges with factory variables, and persists to `session_variables` via `src/gobby/agents/spawn.py:157`. No schema change needed.

Targets:
- `src/gobby/install/shared/workflows/pipelines/front-half-orchestrator.yaml:79-90` — extend `dispatch_planner` step arguments:
  ```yaml
  arguments:
    prompt: "${{ steps.tick.output.dispatch.prompt }}"
    agent: "${{ inputs.planner_agent }}"
    task_id: "${{ steps.tick.output.dispatch.task_id }}"
    provider: "${{ inputs.planner_provider }}"
    model: "${{ inputs.planner_model }}"
    parent_session_id: "${{ session_id }}"
    initial_variables:                                               # NEW
      artifact_path: "${{ steps.tick.output.artifacts.plan_file }}"  # NEW
  ```

No changes to `_front_half.py` (already emits `artifacts.plan_file`). No changes to `planner.yaml`. No changes to `spawn.py:157`.

Preconditions for unified rule's Path B (verify at implementation time):
- `_agent_type = "planner"` — set by `spawn_agent` per agent definition name (existing).
- `assigned_task_id` — set by `planner.yaml:23` / step_variables + spawn-time binding (existing).
- `artifact_path` — set by the new `initial_variables` plumbing above (new).

Verification: integration test spawns the front-half pipeline end-to-end on a seeded parent task; inspects the planner session's `session_variables` and asserts `artifact_path` is present and matches the pipeline's plan file. Then asserts the rule blocks a write outside the artifact path and permits a write to the artifact path.

## Overall verification checklist

- [ ] `uv run pytest tests/workflows/test_is_current_plan_artifact_helper.py tests/workflows/test_condition_helpers_task_status.py tests/workflows/test_plan_artifact_edit_scope_rule.py -v`
- [ ] `uv run pytest tests/skills/test_plan_skill_delegated_mode.py -v`
- [ ] Daemon startup: `workflow_definitions` table contains `block-writes-outside-plan-artifact` after template sync.
- [ ] Path A manual E2E: `/gobby plan` → answer `[D]elegated` → Round 2+ proceeds without `ExitPlanMode` prompt; Edit on non-plan file blocked; Edit on plan file succeeds.
- [ ] Path B manual E2E: trigger front-half on a seeded task; planner active → Write to non-artifact blocked; task reaches `review_approved` → writes unblocked by this rule.
- [ ] Skill precondition: force `artifact_path` absent → skill aborts at Step 7 entry with clear message.
- [ ] `uv run ruff check src/ && uv run mypy src/` clean.

## Reference

Parent campaign plan: `~/.claude/plans/handoff-interactive-planning-for-twinkly-widget.md` Task 5.
Blocked by: #12044 implementation, #12079 contract migration.
Follow-up after landing: #12129 (stale `test_chat_session_permissions_plan.py` cleanup).
