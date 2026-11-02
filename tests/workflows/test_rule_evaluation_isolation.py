"""Rendering and expression evaluation cannot mutate live rule inputs."""

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2.exceptions import SecurityError

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine.blocked_tool_recovery import is_blocked_tool_recovery_remediation
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit

MUTATIONS = [
    'variables.update({"_handoff_turn_end_pending": true})',
    'project_policy.update({"security_paths": []})',
    'project_policy.security_paths.append("evil")',
    "project_policy.security_paths.pop()",
]


def _variables() -> dict[str, Any]:
    return {"_handoff_turn_end_pending": False, "project_policy": {"security_paths": ["safe"]}}


@pytest.mark.parametrize("mutation", MUTATIONS)
@pytest.mark.parametrize("site", ["inline", "file"])
def test_template_engine_inputs_are_immutable(mutation: str, site: str, tmp_path: Path) -> None:
    variables = _variables()
    original = deepcopy(variables)
    context = {"variables": variables, **variables}
    template = "{{ " + mutation + " }}"
    engine = TemplateEngine(template_dirs=[str(tmp_path)])
    if site == "file":
        (tmp_path / "synthetic.j2").write_text(template)
    with pytest.raises((SecurityError, TypeError)):
        if site == "inline":
            engine.render(template, context)
        else:
            engine.render_file("synthetic.j2", context)
    assert variables == original


@pytest.mark.parametrize("target", ["variables", "project_policy", "project_policy.security_paths"])
def test_expression_helpers_receive_read_only_inputs(target: str) -> None:
    variables = _variables()
    original = deepcopy(variables)

    def mutate(value: Any) -> bool:
        if isinstance(value, list):
            value.append("evil")
        else:
            value.update({"_handoff_turn_end_pending": True, "security_paths": []})
        return True

    evaluator = SafeExpressionEvaluator(
        context={"variables": variables, **variables}, allowed_funcs={"mutate": mutate}
    )
    with pytest.raises(ValueError):
        evaluator.evaluate(f"mutate({target})")
    assert variables == original


@pytest.mark.parametrize("mutation", MUTATIONS)
@pytest.mark.parametrize(
    "site",
    [
        "set_variable",
        "inject_context",
        "set_display_content",
        "observe",
        "mcp_call",
        "rewrite_input",
        "set_permission_response",
        "set_watch_paths",
        "set_worktree_path",
        "set_elicitation",
        "block",
    ],
)
@pytest.mark.asyncio
async def test_rule_template_sites_cannot_mutate_session_state(
    temp_db: HubDatabase, mutation: str, site: str
) -> None:
    template = "{{ " + mutation + " }}"
    effects: dict[str, dict[str, Any]] = {
        "set_variable": {"variable": "ordinary", "value": template},
        "inject_context": {"template": template},
        "set_display_content": {"template": template},
        "observe": {"message": template},
        "mcp_call": {"server": "synthetic", "tool": "read", "arguments": {"value": template}},
        "rewrite_input": {"input_updates": {"command": template}},
        "set_permission_response": {"input_updates": {"command": template}},
        "set_watch_paths": {"watch_paths": [template]},
        "set_worktree_path": {"worktree_path": template},
        "set_elicitation": {"elicitation_content": {"value": template}},
        "block": {"reason": template},
    }
    RuleDefinitionManager(temp_db).create(
        name="synthetic-template",
        tags=["default"],
        source="custom",
        definition_json={"event": "before_tool", "effects": [{"type": site, **effects[site]}]},
    )
    variables = _variables()
    original = deepcopy(variables)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CLAUDE,
        session_id="11111111-1111-4111-8111-111111111111",
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read", "tool_input": {"file_path": "example.py"}},
    )
    try:
        await RuleEngine(temp_db).evaluate(event, session_id=event.session_id, variables=variables)
    except (SecurityError, TypeError, ValueError):
        pass
    assert variables["_handoff_turn_end_pending"] == original["_handoff_turn_end_pending"]
    assert variables["project_policy"] == original["project_policy"]


def test_rejected_variable_target_does_not_render_value(temp_db: HubDatabase) -> None:
    engine = RuleEngine(temp_db)
    calls: list[str] = []

    def render_probe() -> str:
        calls.append("rendered")
        return "value"

    variables = _variables()
    wrote = engine._apply_set_variable(
        RuleEffect(type="set_variable", variable="_agent_type", value="{{ probe() }}"),
        variables,
        {"variables": variables},
        {"probe": render_probe},
    )
    assert wrote is False
    assert calls == []


@pytest.mark.parametrize("site", ["condition", "value"])
@pytest.mark.parametrize("mutation", MUTATIONS)
def test_rule_expressions_cannot_mutate_inputs(
    temp_db: HubDatabase, site: str, mutation: str
) -> None:
    variables = _variables()
    original = deepcopy(variables)
    engine = RuleEngine(temp_db)
    context = {"variables": variables, **variables}
    if site == "condition":
        engine._evaluate_condition(mutation, context, allowed_funcs={})
    else:
        engine._apply_set_variable(
            RuleEffect(type="set_variable", variable="ordinary", value=mutation),
            variables,
            context,
            {},
        )
    assert variables["_handoff_turn_end_pending"] == original["_handoff_turn_end_pending"]
    assert variables["project_policy"] == original["project_policy"]


def test_expression_snapshot_preserves_reads_and_returns_detached_mutable_state() -> None:
    variables = _variables()
    evaluator = SafeExpressionEvaluator(
        context={"variables": variables, **variables},
        allowed_funcs={"isinstance": isinstance, "dict": dict, "list": list},
    )
    variables["project_policy"]["security_paths"].append("later")
    assert evaluator.evaluate("project_policy.security_paths == ['safe']") is True
    assert evaluator.evaluate("isinstance(project_policy, dict)") is True
    assert evaluator.evaluate("isinstance(project_policy.security_paths, list)") is True
    result = evaluator.evaluate_value("{'paths': project_policy.security_paths}")
    result["paths"].append("local")
    assert result == {"paths": ["safe", "local"]}
    assert variables["project_policy"]["security_paths"] == ["safe", "later"]


@pytest.mark.parametrize("field", ["data", "metadata"])
def test_event_containers_are_detached_at_render_boundary(field: str) -> None:
    state = _variables()
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CLAUDE,
        session_id="11111111-1111-4111-8111-111111111111",
        timestamp=datetime.now(UTC),
        data=state,
        metadata=state,
    )
    with pytest.raises((SecurityError, TypeError)):
        TemplateEngine().render(
            "{{ event." + field + ".project_policy.security_paths.append('evil') }}",
            {"event": event},
        )
    assert state == _variables()


@pytest.mark.parametrize("mutation", MUTATIONS)
@pytest.mark.parametrize(
    "site",
    ["pipeline-template", "pipeline-expression", "pipeline-condition", "recovery", "step-value"],
)
def test_shared_workflow_evaluation_consumers_preserve_state(
    temp_db: HubDatabase, mutation: str, site: str
) -> None:
    variables = _variables()
    original = deepcopy(variables)
    context = {"variables": variables, **variables}
    try:
        if site == "pipeline-template":
            StepRenderer(TemplateEngine()).render_string("${{ " + mutation + " }}", context)
        elif site == "pipeline-expression":
            StepRenderer()._resolve_expression(mutation, context)
        elif site == "pipeline-condition":
            condition = mutation.replace("project_policy", "inputs.project_policy").replace(
                "variables", "inputs"
            )
            StepRenderer().should_run_step(
                SimpleNamespace(id="synthetic", condition=condition), {"inputs": variables}
            )
        elif site == "recovery":
            expression = mutation.replace("project_policy", "variables.get('project_policy')")
            variables["_last_blocked_recovery_conditions"] = [expression]
            is_blocked_tool_recovery_remediation(variables, {})
        else:
            RuleEngine(temp_db)._evaluate_step_handler_value(mutation, context, "set_variable")
    except (SecurityError, TypeError, ValueError):
        pass
    assert variables["_handoff_turn_end_pending"] == original["_handoff_turn_end_pending"]
    assert variables["project_policy"] == original["project_policy"]
