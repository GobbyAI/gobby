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


def test_a_literal_container_value_is_written_as_a_fresh_copy(temp_db: HubDatabase) -> None:
    engine = RuleEngine(temp_db)
    effect = RuleEffect(type="set_variable", variable="items", value=[])
    variables = _variables()

    # One parsed effect resets the same session twice, e.g. on consecutive turn starts.
    first = engine._apply_set_variable(effect, variables, {"variables": variables})
    variables["items"].append("written this turn")
    second = engine._apply_set_variable(effect, variables, {"variables": variables})

    assert (first, second) == (True, True)
    assert variables["items"] == []
    assert effect.value == []
    assert variables["items"] is not effect.value


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


def test_event_snapshot_is_reused_across_conditions_and_templates(temp_db: HubDatabase) -> None:
    variables = {**_variables(), "project": {"path": "/tmp/synthetic"}}
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CLAUDE,
        session_id="11111111-1111-4111-8111-111111111111",
        timestamp=datetime.now(UTC),
        data={},
    )
    engine = RuleEngine(temp_db)
    context = engine._build_eval_context(event, variables)
    assert context["variables"] is not variables
    assert context["project_policy"] is context["variables"]["project_policy"]
    seen: list[int] = []

    def observe(value: Any) -> bool:
        seen.append(id(value))
        return True

    for _ in range(10):
        assert (
            engine._evaluate_condition(
                "observe(variables)", context, allowed_funcs={"observe": observe}
            )
            is True
        )
    engine._render_template("{{ observe(variables) }}", context, {"observe": observe})
    assert seen == [id(context["variables"])] * 11
    assert SafeExpressionEvaluator(context, {}).context["event"] is context["event"]


@pytest.mark.parametrize(
    ("template", "types"),
    [
        ("{{ dict.update(value, {'changed': true}) }}", {}),
        ("{{ dict.update(value, {'changed': true}) }}", {"dict": dict}),
        ("{{ list.append(value, 'changed') }}", {"list": list}),
    ],
)
def test_type_called_mutators_are_blocked(template: str, types: dict[str, Any]) -> None:
    value: Any = [] if "list" in types else {"safe": True}
    with pytest.raises(SecurityError):
        TemplateEngine().render(template, {"value": value, **types})


@pytest.mark.asyncio
async def test_event_snapshot_refreshes_effect_writes_without_recopying_other_values(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RuleDefinitionManager(temp_db)
    for priority, when, effect in [
        (1, None, {"type": "set_variable", "variable": "phase", "value": 1}),
        (
            2,
            "variables.get('phase') == 1",
            {"type": "set_variable", "variable": "phase", "value": 2},
        ),
        (3, "phase == 2", {"type": "inject_context", "template": "phase={{ phase }}"}),
    ]:
        manager.create(
            name=f"synthetic-phase-{priority}",
            tags=["default"],
            priority=priority,
            definition_json={"event": "before_tool", "when": when, "effects": [effect]},
        )
    variables: dict[str, Any] = {"records": [{"id": n} for n in range(100)]}
    engine = RuleEngine(temp_db)
    original = engine._evaluate_condition
    seen: list[int] = []

    def observe(condition: str, context: dict[str, Any], *args: Any, **kwargs: Any) -> bool:
        assert context["variables"] is not variables
        seen.append(id(context["variables"]["records"]))
        return original(condition, context, *args, **kwargs)

    monkeypatch.setattr(engine, "_evaluate_condition", observe)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CLAUDE,
        session_id="11111111-1111-4111-8111-111111111111",
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read", "tool_input": {"file_path": "example.py"}},
    )
    response = await engine.evaluate(event, session_id=event.session_id, variables=variables)
    assert variables["phase"] == 2
    assert response.context == "phase=2"
    assert len(seen) == 2
    assert seen[0] == seen[1]


@pytest.mark.parametrize("writer", ["set_variable", "mcp_call"])
@pytest.mark.parametrize("reader", ["inject_context", "block"])
@pytest.mark.asyncio
async def test_sibling_effects_read_completed_variable_writes(
    temp_db: HubDatabase, writer: str, reader: str
) -> None:
    write_effect: dict[str, Any] = (
        {"type": "set_variable", "variable": "phase", "value": True}
        if writer == "set_variable"
        else {
            "type": "mcp_call",
            "server": "synthetic",
            "tool": "read",
            "inject_result": True,
            "success_variable": "phase",
        }
    )
    read_effect = {
        "type": reader,
        "when": "variables.get('phase') is True",
        "reason" if reader == "block" else "template": "{{ variables.phase }}/{{ phase }}",
    }
    RuleDefinitionManager(temp_db).create(
        name="synthetic-siblings",
        tags=["default"],
        definition_json={"event": "before_tool", "effects": [write_effect, read_effect]},
    )

    async def dispatch(*args: Any) -> dict[str, Any]:
        return {"success": True}

    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CLAUDE,
        session_id="11111111-1111-4111-8111-111111111111",
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read", "tool_input": {"file_path": "example.py"}},
    )
    response = await RuleEngine(temp_db, mcp_dispatcher=dispatch).evaluate(
        event,
        session_id=event.session_id,
        variables={"phase": False},
    )
    if reader == "block":
        assert response.decision == "block"
        assert response.reason is not None
        assert response.reason.endswith("True/True")
    else:
        assert response.context == "True/True"


@pytest.mark.asyncio
async def test_proxy_replay_refreshes_rewrite_variable_snapshot(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RuleDefinitionManager(temp_db)
    definitions: list[tuple[int, dict[str, Any]]] = [
        (
            1,
            {
                "effects": [
                    {"type": "rewrite_input", "input_updates": {"command": "echo declarative"}}
                ]
            },
        ),
        (2, {"effects": [{"type": "inject_context", "template": "Snapshot reader"}]}),
        (3, {"effects": [{"type": "proxy_hook", "handler": "synthetic"}]}),
        (
            4,
            {
                "when": "tool_input.get('command') == 'echo proxy' and variables.get('_rewrite_input', {}).get('input_updates', {}).get('command') != 'echo proxy'",
                "effects": [{"type": "block", "reason": "Stale rewrite snapshot"}],
            },
        ),
    ]
    for priority, definition in definitions:
        manager.create(
            name=f"synthetic-replay-{priority}",
            tags=["default"],
            priority=priority,
            definition_json={"event": "before_tool", **definition},
        )
    engine = RuleEngine(temp_db)

    async def rewrite(invocations: Any, event: HookEvent, **kwargs: Any) -> bool:
        event.data["tool_input"]["command"] = "echo proxy"
        return True

    monkeypatch.setattr(engine, "_run_proxy_hooks", rewrite)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        source=SessionSource.CLAUDE,
        session_id="11111111-1111-4111-8111-111111111111",
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": "echo original"}},
    )
    response = await engine.evaluate(event, session_id=event.session_id, variables={})
    assert response.decision == "allow"
    assert response.modified_input == {"command": "echo proxy"}
