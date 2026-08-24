"""Tests for static MCP handler failure-route and ordering checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import AgentDefinitionBody, WorkflowDefinition, WorkflowStep
from gobby.workflows.dry_run import evaluate_agent_definition
from gobby.workflows.handler_route_lint import check_handler_routes

pytestmark = pytest.mark.unit


def _handler(server: str, tool: str, **extra: object) -> dict[str, object]:
    return {
        "server": server,
        "tool": tool,
        "action": "set_variable",
        "variable": "handled",
        "value": True,
        **extra,
    }


def _definition(
    *,
    success: list[dict[str, object]],
    error: list[dict[str, object]],
    variables: dict[str, object] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="handler-routes",
        steps=[WorkflowStep(name="work", on_mcp_success=success, on_mcp_error=error)],
        variables=variables or {},
        exit_condition="current_step == 'work'",
    )


@pytest.mark.asyncio
async def test_evaluator_warns_when_success_handler_has_no_failure_route() -> None:
    definition = _definition(success=[_handler("gobby-tasks", "close_task")], error=[])
    result = await evaluate_agent_definition(
        AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name=definition.name,
            provider="claude",
            step_workflow=AgentStepWorkflowBody(
                steps=definition.steps,
                variables=definition.variables or {},
                exit_condition=definition.exit_condition,
            ),
        )
    )

    finding = next(item for item in result.items if item.code == "MISSING_FAILURE_ROUTE")
    assert finding.level == "warning"
    assert finding.detail == {
        "step": "work",
        "server": "gobby-tasks",
        "tool": "close_task",
    }


def test_matching_failure_route_clears_missing_route_warning() -> None:
    definition = _definition(
        success=[_handler("gobby-tasks", "close_task")],
        error=[_handler("gobby-tasks", "close_task")],
    )

    findings = check_handler_routes(definition)

    assert all(finding.code != "MISSING_FAILURE_ROUTE" for finding in findings)


def test_explicit_stay_policy_clears_missing_route_warning() -> None:
    definition = WorkflowDefinition(
        name="handler-routes",
        steps=[
            WorkflowStep(
                name="work",
                on_mcp_success=[_handler("gobby-tasks", "close_task")],
                mcp_error_policy="stay",
            )
        ],
    )

    findings = check_handler_routes(definition)

    assert all(finding.code != "MISSING_FAILURE_ROUTE" for finding in findings)


def test_unreachable_step_still_requires_failure_route() -> None:
    definition = WorkflowDefinition(
        name="handler-routes",
        steps=[
            WorkflowStep(name="work"),
            WorkflowStep(
                name="unused",
                on_mcp_success=[_handler("gobby-tasks", "close_task")],
            ),
        ],
    )

    findings = check_handler_routes(definition)

    finding = next(item for item in findings if item.code == "MISSING_FAILURE_ROUTE")
    assert finding.detail["step"] == "unused"


def test_error_guard_cannot_depend_on_success_only_assignment() -> None:
    definition = _definition(
        success=[
            _handler(
                "gobby-tasks-ops",
                "record_merge_result",
                variable="merge_result_recorded",
            ),
            _handler("gobby-tasks-ops", "close_linked_github_issue"),
        ],
        error=[
            _handler("gobby-tasks-ops", "record_merge_result"),
            _handler(
                "gobby-tasks-ops",
                "close_linked_github_issue",
                when="vars.get('merge_result_recorded') is True",
            ),
        ],
        variables={"merge_result_recorded": False},
    )

    findings = check_handler_routes(definition)

    finding = next(item for item in findings if item.code == "ORDERING_VAR_UNSATISFIABLE")
    assert finding.detail["variables"] == ["merge_result_recorded"]
    assert finding.detail["reason"] == "assigned only by success handlers"


def test_handler_guard_requires_reachable_variable_assignment() -> None:
    definition = _definition(
        success=[_handler("gobby-tasks", "close_task", when="vars['missing_result'] is True")],
        error=[_handler("gobby-tasks", "close_task")],
    )

    findings = check_handler_routes(definition)

    finding = next(item for item in findings if item.code == "ORDERING_VAR_UNSATISFIABLE")
    assert finding.detail["variables"] == ["missing_result"]
    assert finding.detail["reason"] == "no reachable declaration or handler assignment"


def test_unreachable_assignment_does_not_satisfy_handler_guard() -> None:
    guarded = WorkflowStep(
        name="work",
        on_mcp_success=[_handler("gobby-tasks", "close_task", when="vars.ready")],
        on_mcp_error=[_handler("gobby-tasks", "close_task")],
    )
    unreachable = WorkflowStep(
        name="unused",
        on_mcp_success=[_handler("gobby-tasks", "get_task", variable="ready")],
        on_mcp_error=[_handler("gobby-tasks", "get_task")],
    )
    definition = WorkflowDefinition(name="handler-routes", steps=[guarded, unreachable])

    findings = check_handler_routes(definition)

    finding = next(item for item in findings if item.code == "ORDERING_VAR_UNSATISFIABLE")
    assert finding.detail["variables"] == ["ready"]


def test_bundled_agent_handler_routes_are_clean() -> None:
    agents_dir = (
        Path(__file__).parents[2] / "src" / "gobby" / "install" / "shared" / "workflows" / "agents"
    )
    findings: dict[str, list[str]] = {}
    for path in sorted(agents_dir.glob("*.yaml")):
        agent = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))
        nested = agent.step_workflow
        definition = WorkflowDefinition(
            name=agent.name,
            steps=nested.steps if nested else [],
            variables=nested.variables if nested else {},
            exit_condition=nested.exit_condition if nested else None,
        )
        codes = [finding.code for finding in check_handler_routes(definition)]
        if codes:
            findings[path.name] = codes

    assert findings == {}
