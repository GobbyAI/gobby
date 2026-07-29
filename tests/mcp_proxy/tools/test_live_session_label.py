"""Authorization tests for the live-session task label."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._live_session_label import live_session_label_change_error
from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _context(
    *,
    session_type: str = "terminal",
    agent_run_id: str | None = None,
    agent_depth: int = 0,
    loaded_skills: object = ("live-session",),
) -> RegistryContext:
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            resolve_session_id=MagicMock(return_value=SESSION_ID),
            session_manager=SimpleNamespace(
                get=MagicMock(
                    return_value=SimpleNamespace(
                        session_type=session_type,
                        agent_run_id=agent_run_id,
                        agent_depth=agent_depth,
                    )
                )
            ),
            session_var_manager=SimpleNamespace(
                get_variables=MagicMock(
                    return_value={
                        "loaded_skills": (
                            list(loaded_skills)
                            if isinstance(loaded_skills, tuple)
                            else loaded_skills
                        )
                    }
                )
            ),
        ),
    )
    return ctx


def test_authorizes_root_terminal_with_live_session_skill() -> None:
    ctx = _context()

    error = live_session_label_change_error(
        ctx,
        [],
        ["live-session"],
        session_id=SESSION_ID,
    )

    assert error is None


@pytest.mark.parametrize(
    ("session_type", "agent_run_id", "agent_depth", "loaded_skills", "expected_error"),
    [
        pytest.param(
            "agent",
            None,
            0,
            ("live-session",),
            "interactive terminal",
            id="non-terminal",
        ),
        pytest.param(
            "terminal",
            "run-1",
            0,
            ("live-session",),
            "Spawned and automated",
            id="agent-run",
        ),
        pytest.param(
            "terminal",
            None,
            1,
            ("live-session",),
            "Spawned and automated",
            id="agent-depth",
        ),
        pytest.param(
            "terminal",
            None,
            0,
            [],
            "Load the live-session skill",
            id="skill-not-loaded",
        ),
    ],
)
def test_rejects_unauthorized_session_shapes(
    session_type: str,
    agent_run_id: str | None,
    agent_depth: int,
    loaded_skills: object,
    expected_error: str,
) -> None:
    ctx = _context(
        session_type=session_type,
        agent_run_id=agent_run_id,
        agent_depth=agent_depth,
        loaded_skills=loaded_skills,
    )
    error = live_session_label_change_error(
        ctx,
        [],
        ["live-session"],
        session_id=SESSION_ID,
    )

    assert error is not None
    assert expected_error in error


def test_unchanged_membership_does_not_require_session_context() -> None:
    ctx = _context()

    error = live_session_label_change_error(ctx, ["live-session"], ["live-session", "other"])

    assert error is None
    cast(MagicMock, ctx.resolve_session_id).assert_not_called()


@pytest.mark.parametrize(
    "error",
    [ValueError("unavailable"), psycopg.OperationalError("database unavailable")],
)
def test_fails_closed_when_session_variables_cannot_be_read(error: Exception) -> None:
    ctx = _context()
    cast(MagicMock, ctx.session_var_manager.get_variables).side_effect = error

    validation_error = live_session_label_change_error(
        ctx,
        [],
        ["live-session"],
        session_id=SESSION_ID,
    )

    assert validation_error is not None
    assert "readable session state" in validation_error


@pytest.mark.asyncio
async def test_create_task_routes_live_session_label_through_guard(
    mock_task_manager: MagicMock,
    sample_task: Task,
) -> None:
    registry = create_task_registry(mock_task_manager)

    with (
        patch(
            "gobby.utils.session_context.get_current_session_id",
            return_value=SESSION_ID,
        ),
        patch.object(RegistryContext, "resolve_session_id", return_value=SESSION_ID),
        patch.object(
            RegistryContext,
            "resolve_project_from_session",
            return_value=sample_task.project_id,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._crud.live_session_label_change_error",
            return_value="guard rejected label",
        ) as guard,
    ):
        result = await registry.call(
            "create_task",
            {
                "title": "Live umbrella",
                "category": "research",
                "labels": ["live-session"],
                "validation_criteria": "Test task completion is observable.",
            },
        )

    assert result == {"error": "guard rejected label"}
    guard.assert_called_once()
    guard_args, guard_kwargs = guard.call_args
    assert guard_args[1] == ()
    assert guard_args[2] == ["live-session"]
    assert guard_kwargs["session_id"] == SESSION_ID
    mock_task_manager.create_task_with_decomposition.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "existing_labels", "manager_method"),
    [
        pytest.param(
            "update_task",
            {"task_id": SESSION_ID, "labels": ["test", "live-session"]},
            ["test"],
            "update_task",
            id="update",
        ),
        pytest.param(
            "add_label",
            {"task_id": SESSION_ID, "label": "live-session"},
            ["test"],
            "add_label",
            id="add-label",
        ),
        pytest.param(
            "remove_label",
            {"task_id": SESSION_ID, "label": "live-session"},
            ["live-session"],
            "remove_label",
            id="remove-label",
        ),
    ],
)
async def test_all_mutation_entry_points_reject_missing_session_context(
    mock_task_manager: MagicMock,
    sample_task: Task,
    tool_name: str,
    arguments: dict[str, object],
    existing_labels: list[str],
    manager_method: str,
) -> None:
    existing_task = replace(sample_task, labels=existing_labels)
    mock_task_manager.get_task.return_value = existing_task
    registry = create_task_registry(mock_task_manager)

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._context.get_project_context",
            return_value={"id": sample_task.project_id},
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._live_session_label.get_current_session_id",
            return_value=None,
        ),
    ):
        result = await registry.call(tool_name, arguments)

    assert "active session context" in result["error"]
    getattr(mock_task_manager, manager_method).assert_not_called()
