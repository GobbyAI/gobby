"""Tests for active step workflow context helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.workflows.step_context import (
    first_incomplete_step_workflow,
    get_active_step_workflow_context,
)

pytestmark = pytest.mark.unit


def _patch_step_workflow_managers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instances: list[SimpleNamespace],
    definition_json: str,
    session_variables: dict[str, object] | None = None,
) -> None:
    instance_manager = MagicMock()
    instance_manager.get_active_instances.return_value = instances
    definition_manager = MagicMock()
    definition_manager.get_by_name.return_value = SimpleNamespace(
        workflow_type="step",
        definition_json=definition_json,
    )
    variable_manager = MagicMock()
    variable_manager.get_variables.return_value = session_variables or {}
    monkeypatch.setattr(
        "gobby.workflows.step_context.WorkflowInstanceManager",
        lambda _db: instance_manager,
    )
    monkeypatch.setattr(
        "gobby.workflows.step_context.LocalWorkflowDefinitionManager",
        lambda _db: definition_manager,
    )
    monkeypatch.setattr(
        "gobby.workflows.state_manager.SessionVariableManager",
        lambda _db: variable_manager,
    )


_TERMINATE_DEFINITION = (
    '{"name": "expansion-qa-steps", "steps": [{"name": "qa_check"}, {"name": "terminate"}], '
    '"exit_condition": "current_step == \'terminate\'"}'
)


def test_first_incomplete_step_workflow_returns_none_when_exit_condition_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_workflow_managers(
        monkeypatch,
        instances=[
            SimpleNamespace(
                workflow_name="expansion-qa-steps",
                current_step="terminate",
                variables={},
            )
        ],
        definition_json=_TERMINATE_DEFINITION,
    )

    assert first_incomplete_step_workflow(MagicMock(), "session-1") is None


def test_first_incomplete_step_workflow_reports_unmet_exit_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_workflow_managers(
        monkeypatch,
        instances=[
            SimpleNamespace(
                workflow_name="expansion-qa-steps",
                current_step="qa_check",
                variables={},
            )
        ],
        definition_json=_TERMINATE_DEFINITION,
    )

    incomplete = first_incomplete_step_workflow(MagicMock(), "session-1")

    assert incomplete is not None
    assert incomplete.workflow_name == "expansion-qa-steps"
    assert incomplete.current_step == "qa_check"
    assert incomplete.eval_error is None


def test_first_incomplete_step_workflow_reports_missing_exit_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_workflow_managers(
        monkeypatch,
        instances=[
            SimpleNamespace(
                workflow_name="loose-steps",
                current_step="review",
                variables={},
            )
        ],
        definition_json='{"name": "loose-steps", "steps": [{"name": "review"}]}',
    )

    incomplete = first_incomplete_step_workflow(MagicMock(), "session-1")

    assert incomplete is not None
    assert incomplete.exit_condition is None


def test_first_incomplete_step_workflow_carries_evaluation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_workflow_managers(
        monkeypatch,
        instances=[
            SimpleNamespace(
                workflow_name="broken-steps",
                current_step="review",
                variables={},
            )
        ],
        definition_json=(
            '{"name": "broken-steps", "steps": [{"name": "review"}], '
            '"exit_condition": "current_step ==="}'
        ),
    )

    incomplete = first_incomplete_step_workflow(MagicMock(), "session-1")

    assert incomplete is not None
    assert incomplete.eval_error is not None


def test_first_incomplete_step_workflow_honors_step_workflow_complete_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_step_workflow_managers(
        monkeypatch,
        instances=[
            SimpleNamespace(
                workflow_name="expansion-qa-steps",
                current_step="qa_check",
                variables={},
            )
        ],
        definition_json=_TERMINATE_DEFINITION,
        session_variables={"step_workflow_complete": True},
    )

    assert first_incomplete_step_workflow(MagicMock(), "session-1") is None


class _FailingDb:
    def fetchall(self, *_args: object, **_kwargs: object) -> list[object]:
        raise psycopg.DatabaseError("database unavailable")


def test_get_active_step_workflow_context_propagates_db_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Synchronous DB driver failures propagate without duplicate local logging."""
    # A valid uuid session id is required to get past the is_session_uuid
    # guard so the lookup actually reaches the (failing) database.
    with pytest.raises(psycopg.DatabaseError):
        get_active_step_workflow_context(
            _FailingDb(),  # type: ignore[arg-type]
            "11111111-1111-4111-8111-111111111111",
        )

    assert not caplog.records


@pytest.mark.parametrize(
    ("definition_json", "expected_log"),
    [
        ("{not-json", "invalid JSON"),
        ('{"name": "workflow", "steps": "bad"}', "validation failed"),
    ],
)
def test_get_active_step_workflow_context_logs_malformed_definitions(
    definition_json: str,
    expected_log: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed step workflow definitions are visible in warning logs."""
    instance_manager = MagicMock()
    instance_manager.get_active_instances.return_value = [
        SimpleNamespace(workflow_name="workflow", current_step="review")
    ]
    definition_manager = MagicMock()
    definition_manager.get_by_name.return_value = SimpleNamespace(
        workflow_type="step",
        definition_json=definition_json,
    )
    monkeypatch.setattr(
        "gobby.workflows.step_context.WorkflowInstanceManager",
        lambda _db: instance_manager,
    )
    monkeypatch.setattr(
        "gobby.workflows.step_context.LocalWorkflowDefinitionManager",
        lambda _db: definition_manager,
    )

    with caplog.at_level("WARNING", logger="gobby.workflows.step_context"):
        result = get_active_step_workflow_context(MagicMock(), "session-1")

    assert result is None
    assert "Skipping malformed step workflow definition workflow" in caplog.text
    assert expected_log in caplog.text
