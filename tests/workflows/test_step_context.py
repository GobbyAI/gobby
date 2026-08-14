"""Tests for active step workflow context helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep
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
