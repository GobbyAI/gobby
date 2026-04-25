# Wire task_manager into RuleEngine Condition Evaluation

## Overview

Production RuleEngine condition evaluation silently no-ops three task helpers — `task_tree_complete`, `task_needs_human_review`, `task_has_label_prefix` — because `TemplatingMixin._build_allowed_funcs` calls `build_condition_helpers(context=ctx)` with no `task_manager`, and `build_condition_helpers` falls back to stub closures (`True`, `False`, `False`) when `task_manager is None`. Four installed rules depend on these helpers and never fire in production. Thread the existing `LocalTaskManager` (already in scope inside `HookManagerFactory` as `storage.task`) through `RuleEngine.__init__` into `TemplatingMixin._build_allowed_funcs` and onward into `build_condition_helpers(task_manager=...)` so the production condition path evaluates against real task state.

A second, related bug blocks one of the four rules from working even after the wiring is fixed: `_build_eval_context` unwraps `call_tool` / `mcp__gobby__call_tool` inputs down to the inner `arguments` payload (`src/gobby/workflows/engine/templating.py:47-63`), re-injecting only `server_name` and `tool_name`. The `block-front-half-on-interactive-lock` rule reads `(tool_input.get('arguments') or {}).get('task_id')` — which is `None` post-unwrap. Fix the rule to read the unwrapped shape at the same time as the wiring; otherwise the wiring appears correct but the named regression remains unfixed.

## Constraints

- **The behavior change is the fix, not collateral damage.** Once wired, these four rules transition from stubbed no-ops to real evaluation against live task state:
  - `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml` — will block `turn_end` when a claimed epic has open subtasks (currently permits all stops).
  - `src/gobby/install/shared/workflows/rules/auto-task/notify-task-tree-complete.yaml` — will only inject the completion notice when the autonomous task tree is truly complete (currently fires on every qualifying `turn_end`).
  - `src/gobby/install/shared/workflows/rules/auto-task/guide-task-continuation.yaml` — will only block `turn_end` when the autonomous task still has open work (currently blocks unconditionally while `stop_attempts < max`).
  - `src/gobby/install/shared/workflows/rules/task-enforcement/block-front-half-on-interactive-lock.yaml` — will actually block `front_half_tick` when the parent carries any `interactive:planning-in-progress:*` label (currently never blocks; also requires the input-shape fix below).
- `RuleEngine.__init__`'s new `task_manager` parameter must default to `None`. There are 150+ existing test instantiations of `RuleEngine(db)` across 12 test files that rely on this default — they must continue to pass unchanged.
- `build_condition_helpers(task_manager=None)` retains its stub-fallback branch. It is a helper-level contract for defensive callers and is unit-tested by `tests/workflows/test_safe_evaluator.py::test_no_task_manager_returns_true`; that test stays as-is.
- **Do NOT add tests to existing monolith files.** Per guiding principle #2 (files under 1000 lines), `tests/workflows/test_rule_engine.py` (2695 lines), `tests/workflows/test_hooks.py` (1569 lines), and `tests/workflows/test_stop_gates_rules.py` (1383 lines) are already over the threshold with no open refactor tasks. Route all new verification into the test files listed under § 1.1's *Verification scenarios*, which are all well under 1000 lines and stay that way after the additions.
- Pipeline condition evaluation is **out of scope**. `src/gobby/workflows/pipeline/renderer.py` constructs `SafeExpressionEvaluator` with a separate `_PIPELINE_EVAL_FUNCS` dict and does not call `build_condition_helpers`. Nothing in the pipeline path is affected.

## Phase 1: Wire task_manager through RuleEngine

**Goal**: `build_condition_helpers` receives a real `LocalTaskManager` when invoked via the production rule-evaluation path, and all four dependent rules evaluate correctly against real task state (including the previously-masked input-shape bug in `block-front-half-on-interactive-lock`).

### 1.1 Thread task_manager through RuleEngine and fix the front-half-lock input shape [category: code]

Targets:

- `src/gobby/workflows/engine/core.py` — `RuleEngine.__init__`
- `src/gobby/workflows/engine/templating.py` — `TemplatingMixin._build_allowed_funcs`
- `src/gobby/hooks/factory.py` — `HookManagerFactory._create_workflow_engine`
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-front-half-on-interactive-lock.yaml` — condition-expression input shape

There is exactly one production call site for `build_condition_helpers` (`src/gobby/workflows/engine/templating.py:108`) and one production construction site for `RuleEngine` (`src/gobby/hooks/factory.py:465`). The fix is four coordinated edits.

**Change 1 — `src/gobby/workflows/engine/core.py` (lines 139-151):**

Add `task_manager: Any | None = None` as the last kw-arg on `RuleEngine.__init__` and store it as `self._task_manager`. New-arg-last preserves positional call compatibility for every `RuleEngine(db)` / `RuleEngine(db=db)` test instantiation.

```python
class RuleEngine(EffectsMixin, TemplatingMixin, EnforcementMixin):
    """Single-pass rule evaluation engine.

    Loads rules from workflow_definitions (workflow_type='rule'),
    applies session overrides, evaluates in priority order.
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        skill_manager: Any | None = None,
        metrics_event_store: "MetricsEventStore | None" = None,
        mcp_dispatcher: Any | None = None,
        task_manager: Any | None = None,
    ):
        self.db = db
        self.definition_manager = LocalWorkflowDefinitionManager(db)
        self.instance_manager = WorkflowInstanceManager(db)
        self._skill_manager = skill_manager
        self._event_store = metrics_event_store
        self._mcp_dispatcher = mcp_dispatcher
        self._task_manager = task_manager
```

**Change 2 — `src/gobby/workflows/engine/templating.py` (lines 29-32, 105-119):**

`TemplatingMixin` is a mixin — it gets `self._task_manager` from the concrete `RuleEngine` class. Declare the attribute at the top of the class alongside the existing `db: DatabaseProtocol` protocol declaration (same pattern already used for cross-mixin protocol access in this file). Then forward it into `build_condition_helpers`:

```python
class TemplatingMixin:
    """Mixin providing templating and condition evaluation methods for RuleEngine."""

    db: DatabaseProtocol
    _task_manager: Any | None  # provided by the concrete RuleEngine class

    # ... _build_eval_context unchanged ...

    def _build_allowed_funcs(self, ctx: dict[str, Any]) -> dict[str, Callable[..., Any]]:
        """Build the shared helper-function dict for condition evaluation and template rendering."""
        variables = ctx.get("variables", {})
        funcs = build_condition_helpers(
            task_manager=getattr(self, "_task_manager", None),
            context=ctx,
        )
        funcs["isinstance"] = isinstance
        funcs["is_server_listed"] = lambda ti: is_server_listed(ti, variables)
        funcs["is_tool_unlocked"] = lambda ti: is_tool_unlocked(ti, variables)
        funcs["is_discovery_tool"] = is_discovery_tool
        funcs["is_plan_file"] = is_plan_file
        funcs["get_touched_file_paths"] = get_touched_file_paths
        funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file
        funcs["is_message_delivery_tool"] = is_message_delivery_tool
        funcs["has_pending_messages"] = self._has_pending_messages
        funcs["pending_message_count"] = self._pending_message_count
        return funcs
```

`getattr(self, "_task_manager", None)` (rather than `self._task_manager`) keeps the mixin safely callable if another consumer ever instantiates it standalone without the attribute — the production consumer (`RuleEngine`) always sets it.

**Change 3 — `src/gobby/hooks/factory.py` (lines 465-470):**

Pass the existing `storage.task` when constructing the engine. `storage.task` is a `LocalTaskManager` built at `_create_storage` (~line 398) and already passed separately to `WorkflowHookHandler(task_manager=storage.task, ...)` at line 506, so no new wiring or scope change is needed — only the new constructor argument:

```python
rule_engine = RuleEngine(
    db=database,
    skill_manager=skill_manager,
    metrics_event_store=metrics_event_store,
    mcp_dispatcher=inline_dispatcher,
    task_manager=storage.task,
)
```

**Change 4 — `src/gobby/install/shared/workflows/rules/task-enforcement/block-front-half-on-interactive-lock.yaml` (lines 9-16):**

`_build_eval_context` unwraps `call_tool` MCP inputs before condition evaluation: when `event.data.tool_name in ("call_tool", "mcp__gobby__call_tool")`, `tool_input` is replaced with the inner `arguments` dict (with only `server_name` and `tool_name` re-injected from the outer shape). The current rule reads `(tool_input.get('arguments') or {}).get('task_id')`, which assumes the un-unwrapped shape and therefore always resolves to `None`. Rewrite the condition to read the unwrapped shape directly:

```yaml
when: >-
  event.data.get('tool_name') in ('call_tool', 'mcp__gobby__call_tool')
  and tool_input.get('server_name') == 'gobby-tasks-ops'
  and tool_input.get('tool_name') == 'front_half_tick'
  and task_has_label_prefix(
    tool_input.get('task_id'),
    'interactive:planning-in-progress:'
  )
```

Bundled rule templates are synced to `workflow_definitions` with hash-based drift detection (`src/gobby/install/shared/CLAUDE.md`) — changing the YAML alone is not enough if a stale DB row exists; the sync picks up the new hash on daemon start for rows that haven't been hand-edited, but any hand-edited override would need reconciliation. Document this in the commit message and confirm the row is template-synced (not overridden) in the active DB before closing.

**Verification scenarios** (for the auto-generated TDD wrapper to implement as failing tests first):

Target files (all ≤ 1000 lines — see Constraints for the do-not-touch list):

- `tests/workflows/test_task_enforcement_rules.py` (888 lines) — F1 integration scenario.
- `tests/workflows/test_auto_task_rules.py` (200 lines) — F2 auto-task scenarios.
- **NEW** `tests/workflows/test_rule_engine_task_manager_wiring.py` — require-epic-tree-close integration, factory wiring assertion, backward-compat check. Create this file; do **not** route these into `test_rule_engine.py`.

All four integration scenarios below must go through `RuleEngine.evaluate()` end-to-end, not through a hand-built `SafeExpressionEvaluator` with a synthesized `allowed_funcs`. The point is to catch bugs like Change 4 that only surface when the real `_build_eval_context` unwrap runs before condition eval.

1. **`block-front-half-on-interactive-lock` real-label + real-unwrap test** → `tests/workflows/test_task_enforcement_rules.py`. Construct `RuleEngine(db, task_manager=task_manager)` with the test fixtures' `LocalTaskManager`. Rely on the bundled rule sync to load `block-front-half-on-interactive-lock` into `workflow_definitions`. Create a task, add label `interactive:planning-in-progress:s1`. Feed a `before_tool` `HookEvent` whose `data.tool_name == "call_tool"` and `data.tool_input == {"server_name": "gobby-tasks-ops", "tool_name": "front_half_tick", "arguments": {"task_id": <ref>}}` (outer shape — let `_build_eval_context` do the unwrap). Assert the response blocks. Remove the label; re-evaluate; assert the response does **not** block. This single test catches both the wiring bug and the input-shape bug.
2. **`guide-task-continuation` real-state test** → `tests/workflows/test_auto_task_rules.py`. Same engine construction. Create an autonomous task with an open subtask. Set session variables `auto_task_ref=<ref>`, `stop_attempts=0`, `max_stop_attempts=8`. Feed a `turn_end` event. Assert the response blocks. Close the subtask. Re-evaluate. Assert the response does **not** block.
3. **`notify-task-tree-complete` real-state test** → `tests/workflows/test_auto_task_rules.py`. Same engine construction. Create an autonomous task with a closed tree (no open descendants). Set `auto_task_ref=<ref>`. Feed `turn_end`. Assert the rule's `inject_context` effect fires (completion notice is present in the response). Add an open subtask. Re-evaluate. Assert the effect does **not** fire.
4. **`require-epic-tree-close` real-state test + factory wiring + backward-compat** → `tests/workflows/test_rule_engine_task_manager_wiring.py` (new file). Three subtests in one class:
   - *Epic tree gate:* Create an epic with one open child. Set variables `task_claimed=True`, `claimed_tasks={<epic_ref>: {...}}`, `plan_mode=False`, `stop_attempts=0`. Feed `turn_end`. Assert blocks. Close the child. Re-evaluate. Assert does not block.
   - *Factory wiring:* Exercise `HookManagerFactory._create_workflow_engine` (or the public surface that builds it) and assert `rule_engine._task_manager is storage.task`. Locks in the contract so a drive-by refactor can't silently regress the wiring.
   - *Backward compat:* `RuleEngine(db)` with no `task_manager` kw-arg still constructs without raising. Feeding the `before_tool` event from scenario 1 returns a non-blocking response (stub branch of `build_condition_helpers(task_manager=None)` still returns `False` for `task_has_label_prefix`). Protects the 150+ existing `RuleEngine(db)` test instantiations from silent breakage.

Unchanged tests — do **not** modify:

- `tests/workflows/test_safe_evaluator.py::test_no_task_manager_returns_true` — still valid; tests the helper-level contract, which retains its `None` fallback.
- All other `RuleEngine(db)` test instantiations across `test_rule_engine.py`, `test_stop_gates_rules.py`, `test_rewrite_rules.py`, `test_tool_hygiene_rules.py`, `test_agent_scope_rules.py`, `test_hooks.py`, `test_step_enforcement.py`, `test_codex_skill_injection.py`, `test_progressive_discovery_rules.py`, `test_flow_instrumentation.py`, `test_tool_proxy_validation.py` — continue to pass because the new parameter defaults to `None`.

**Risk log (to reproduce in the commit message):**

- Expected production-behavior deltas on merge: `require-epic-tree-close` begins blocking `turn_end` when claimed epics have open subtasks; `guide-task-continuation` stops force-blocking on auto-task turns once the tree is complete; `notify-task-tree-complete` stops injecting its completion notice mid-work; `block-front-half-on-interactive-lock` actually prevents `front_half_tick` when an interactive lock is present. All four are the *point* of the fix — no rollback toggle needed.
- If any test outside the new wiring tests above starts failing after this change, the failing test almost certainly depended on a stub return value. The correct fix is to provide a real (or appropriately-mocked) `task_manager` to that test, not to revert the production default. Investigate and fix forward.
- If a production rule condition relies on a stub return value in a way that was not itemized in the four rules above, the fix reveals a latent bug in that rule's logic rather than introducing a regression. Investigate and fix forward; do not restore the stub path.
- Bundled rule templates only auto-sync on first daemon install; hand-edited DB rows for `block-front-half-on-interactive-lock` will not pick up the YAML change automatically. Verify the active DB row matches the new template hash before closing the task; reconcile any divergence explicitly.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## Adversary Findings — Round 2

No blocking or nit findings after the second pass. The revised plan closes the
round-1 gaps by fixing the post-unwrap `tool_input` shape for
`block-front-half-on-interactive-lock`, adding end-to-end `RuleEngine.evaluate()`
coverage for `guide-task-continuation` and `notify-task-tree-complete`, and
keeping all new tests out of the existing monolith files.
