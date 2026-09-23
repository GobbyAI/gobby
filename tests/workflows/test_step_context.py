"""Tests for active step workflow context helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep, WorkflowTransition
from gobby.workflows.step_context import (
    first_incomplete_step_workflow,
    get_active_step_workflow_context,
)

pytestmark = pytest.mark.unit


def _snapshot(steps: list[str], exit_condition: str | None = None) -> AgentStepWorkflowBody:
    return AgentStepWorkflowBody(
        steps=[WorkflowStep(name=step) for step in steps],
        exit_condition=exit_condition,
    )


def _instance(
    name: str,
    step: str,
    *,
    variables: dict[str, object] | None = None,
    snapshot: AgentStepWorkflowBody | None = None,
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_name=name,
        current_step=step,
        variables=variables or {},
        enabled=enabled,
        snapshot=snapshot or _snapshot([step], None),
    )


def _patch_step_instance(
    monkeypatch: pytest.MonkeyPatch,
    instance: SimpleNamespace | None,
    *,
    session_variables: dict[str, object] | None = None,
) -> None:
    instance_manager = MagicMock()
    instance_manager.get_for_session.return_value = instance
    variable_manager = MagicMock()
    variable_manager.get_variables.return_value = session_variables or {}
    monkeypatch.setattr(
        "gobby.workflows.step_context.AgentStepInstanceManager",
        lambda _db: instance_manager,
    )
    monkeypatch.setattr(
        "gobby.workflows.state_manager.SessionVariableManager",
        lambda _db: variable_manager,
    )


def test_first_incomplete_step_workflow_returns_none_when_exit_condition_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(
        monkeypatch,
        _instance(
            "expansion-qa",
            "terminate",
            snapshot=_snapshot(["qa_check", "terminate"], "current_step == 'terminate'"),
        ),
    )
    assert first_incomplete_step_workflow(MagicMock(), "session-1") is None


def test_first_incomplete_step_workflow_reports_unmet_exit_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(
        monkeypatch,
        _instance(
            "expansion-qa",
            "qa_check",
            snapshot=_snapshot(["qa_check", "terminate"], "current_step == 'terminate'"),
        ),
    )
    incomplete = first_incomplete_step_workflow(MagicMock(), "session-1")
    assert incomplete is not None
    assert incomplete.workflow_name == "expansion-qa"
    assert incomplete.current_step == "qa_check"
    assert incomplete.eval_error is None


def test_first_incomplete_step_workflow_reports_missing_exit_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(
        monkeypatch,
        _instance("loose", "review", snapshot=_snapshot(["review"], None)),
    )
    incomplete = first_incomplete_step_workflow(MagicMock(), "session-1")
    assert incomplete is not None
    assert incomplete.exit_condition is None


def test_first_incomplete_step_workflow_carries_evaluation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(
        monkeypatch,
        _instance(
            "broken",
            "review",
            snapshot=_snapshot(["review"], "current_step ==="),
        ),
    )
    incomplete = first_incomplete_step_workflow(MagicMock(), "session-1")
    assert incomplete is not None
    assert incomplete.eval_error is not None


def test_first_incomplete_step_workflow_honors_step_workflow_complete_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(
        monkeypatch,
        _instance(
            "expansion-qa",
            "qa_check",
            snapshot=_snapshot(["qa_check", "terminate"], "current_step == 'terminate'"),
        ),
        session_variables={"step_workflow_complete": True},
    )
    assert first_incomplete_step_workflow(MagicMock(), "session-1") is None


class _FailingDb:
    def fetchone(self, *_args: object, **_kwargs: object) -> object:
        raise psycopg.DatabaseError("database unavailable")


def test_get_active_step_workflow_context_propagates_db_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(psycopg.DatabaseError):
        get_active_step_workflow_context(
            _FailingDb(),  # type: ignore[arg-type]
            "11111111-1111-4111-8111-111111111111",
        )
    assert not caplog.records


def test_get_active_step_workflow_context_skips_unknown_current_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(
        monkeypatch,
        _instance("coder", "missing", snapshot=_snapshot(["claim"], "done")),
    )
    assert get_active_step_workflow_context(MagicMock(), "session-1") is None


def test_first_incomplete_step_workflow_skips_missing_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(monkeypatch, None)
    assert first_incomplete_step_workflow(MagicMock(), "session-1") is None


def _mcp_handler_step(
    name: str,
    *,
    transitions: list[WorkflowTransition] | None = None,
    exit_when: str | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        name=name,
        on_mcp_success=[
            {
                "server": "gobby-agents",
                "tool": "end_agent_run",
                "action": "set_variable",
                "variable": "review_complete",
                "value": True,
            }
        ],
        transitions=transitions or [],
        exit_when=exit_when,
    )


def _step_snapshot(step: WorkflowStep, exit_condition: str | None) -> AgentStepWorkflowBody:
    return AgentStepWorkflowBody(steps=[step], exit_condition=exit_condition)


def test_step_gated_only_on_mcp_success_is_flagged_mcp_progress_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task-close-reviewer review step: allowed_tools 'all', MCP-only exit."""
    _patch_step_instance(
        monkeypatch,
        _instance(
            "task-close-reviewer",
            "review",
            snapshot=_step_snapshot(_mcp_handler_step("review"), "vars.review_complete"),
        ),
    )

    context = get_active_step_workflow_context(MagicMock(), "session-1")

    assert context is not None
    assert context.allowed_tools == "all"
    assert context.mcp_progress_only is True


def test_step_with_transitions_is_not_mcp_progress_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared transition is a non-MCP route forward, so the guard stays off.

    This covers the `not step.transitions` conjunct on its own. The watchdog
    tests construct `StepWorkflowContext` with `mcp_progress_only` preloaded,
    so extraction is only exercised here.
    """
    step = _mcp_handler_step(
        "review",
        transitions=[WorkflowTransition(to="report", when="vars.findings")],
    )
    _patch_step_instance(
        monkeypatch,
        _instance(
            "task-close-reviewer",
            "review",
            snapshot=_step_snapshot(step, "vars.review_complete"),
        ),
    )

    context = get_active_step_workflow_context(MagicMock(), "session-1")

    assert context is not None
    assert step.transitions
    assert step.exit_when is None
    assert context.mcp_progress_only is False


def test_step_with_exit_when_is_not_mcp_progress_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`exit_when` is the other non-MCP route, covered independently."""
    step = _mcp_handler_step("review", exit_when="vars.done")
    _patch_step_instance(
        monkeypatch,
        _instance(
            "task-close-reviewer",
            "review",
            snapshot=_step_snapshot(step, "vars.review_complete"),
        ),
    )

    context = get_active_step_workflow_context(MagicMock(), "session-1")

    assert context is not None
    assert not step.transitions
    assert context.mcp_progress_only is False


def test_step_without_mcp_handlers_is_not_mcp_progress_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_instance(
        monkeypatch,
        _instance(
            "coder",
            "implement",
            snapshot=_step_snapshot(WorkflowStep(name="implement"), "vars.done"),
        ),
    )

    context = get_active_step_workflow_context(MagicMock(), "session-1")

    assert context is not None
    assert context.mcp_progress_only is False
