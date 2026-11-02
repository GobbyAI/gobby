"""Authorization tests for the live-session task label."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._live_session_label import live_session_label_change_error
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("tool", ["update_task", "remove_label"])
def test_concurrent_security_label_addition_survives_authorized_snapshot(
    temp_db: HubDatabase, sample_project: dict[str, str], tool: str
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Concurrent label change")
    registry = create_task_registry(manager)
    checked, writer_done = Event(), Event()

    def guard(
        ctx: RegistryContext, before: Iterable[str] | None, after: Iterable[str] | None
    ) -> str | None:
        error = live_session_label_change_error(ctx, before, after)
        checked.set()
        writer_done.wait(timeout=1)
        return error

    def add_security() -> None:
        assert checked.wait(timeout=5)
        manager.add_label(task.id, "security-review")
        writer_done.set()

    module = "_crud" if tool == "update_task" else "_lifecycle_labels"
    arguments: dict[str, object] = {"task_id": task.id}
    if tool == "update_task":
        arguments["labels"] = []
    else:
        arguments["label"] = "security-review"
    with (
        patch(f"gobby.mcp_proxy.tools.tasks.{module}.live_session_label_change_error", guard),
        patch(
            "gobby.mcp_proxy.tools.tasks._context.get_project_context",
            return_value={"id": sample_project["id"]},
        ),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        mutation = pool.submit(registry.call_sync, tool, arguments)
        addition = pool.submit(add_security)
        mutation.result(timeout=10)
        addition.result(timeout=10)
    assert "security-review" in (manager.get_task(task.id).labels or [])


def _context(
    *,
    session_type: str = "terminal",
    agent_run_id: str | None = None,
    agent_depth: int = 0,
    loaded_skill_references: object = ("gobby:references/tasks/live-work.md",),
) -> RegistryContext:
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(db=MagicMock()),
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
                        "loaded_skill_references": (
                            list(loaded_skill_references)
                            if isinstance(loaded_skill_references, tuple)
                            else loaded_skill_references
                        )
                    }
                )
            ),
        ),
    )
    return ctx


@pytest.mark.parametrize("authority", ["operator", "capability", "reviewer"])
def test_security_label_removal_requires_explicit_authority(authority: str) -> None:
    from gobby.workflows.agent_models import AgentDefinitionBody

    definition = AgentDefinitionBody(
        name="arbitrary-seat",
        prompts={"agent": "Review"},
        workflows={"rule_selectors": {"include": []}},
        capabilities=["remove_security_review_label"] if authority == "capability" else [],
    )
    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._live_session_label.session_agent_definition",
            return_value=(definition, None),
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._live_session_label.is_operator_session",
            return_value=authority == "operator",
        ),
    ):
        error = live_session_label_change_error(
            _context(), ["security-review"], [], session_id=SESSION_ID
        )
    assert (error is None) is (authority != "reviewer")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["update_task", "remove_label"])
async def test_security_label_removal_entry_points_require_identity(
    mock_task_manager: MagicMock, sample_task: Task, tool: str
) -> None:
    mock_task_manager.get_task.return_value = replace(sample_task, labels=["security-review"])
    registry = create_task_registry(mock_task_manager)
    arguments: dict[str, object] = {
        "task_id": SESSION_ID,
        **({"labels": []} if tool == "update_task" else {"label": "security-review"}),
    }
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
        result = await registry.call(tool, arguments)
    assert "authenticated operator" in result["error"]
    getattr(mock_task_manager, tool).assert_not_called()


def test_authorizes_root_terminal_with_live_work_reference() -> None:
    ctx = _context()

    error = live_session_label_change_error(
        ctx,
        [],
        ["live-session"],
        session_id=SESSION_ID,
    )

    assert error is None


@pytest.mark.parametrize(
    ("session_type", "agent_run_id", "agent_depth", "loaded_skill_references", "expected_error"),
    [
        pytest.param(
            "agent",
            None,
            0,
            ("gobby:references/tasks/live-work.md",),
            "interactive terminal",
            id="non-terminal",
        ),
        pytest.param(
            "terminal",
            "run-1",
            0,
            ("gobby:references/tasks/live-work.md",),
            "Spawned and automated",
            id="agent-run",
        ),
        pytest.param(
            "terminal",
            None,
            1,
            ("gobby:references/tasks/live-work.md",),
            "Spawned and automated",
            id="agent-depth",
        ),
        pytest.param(
            "terminal",
            None,
            0,
            [],
            "references/tasks/live-work.md",
            id="skill-not-loaded",
        ),
    ],
)
def test_rejects_unauthorized_session_shapes(
    session_type: str,
    agent_run_id: str | None,
    agent_depth: int,
    loaded_skill_references: object,
    expected_error: str,
) -> None:
    ctx = _context(
        session_type=session_type,
        agent_run_id=agent_run_id,
        agent_depth=agent_depth,
        loaded_skill_references=loaded_skill_references,
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


@pytest.mark.parametrize(
    "variables, authorized",
    [
        ({"loaded_skills": ["gobby"]}, False),
        ({"loaded_skills": ["live-session"]}, False),
        ({"loaded_skills": ["gobby:references/tasks/live-work.md"]}, False),
        ({"loaded_skill_references": ["gobby:references/tasks/overview.md"]}, False),
        ({"loaded_skill_references": ["gobby:references/tasks/live-work.md"]}, True),
    ],
)
def test_reference_contract_4_1_3(variables: dict[str, list[str]], authorized: bool) -> None:
    """Only completed live-work guidance satisfies interactive label authorization."""
    ctx = _context()
    assert ctx.session_var_manager is not None
    cast(MagicMock, ctx.session_var_manager.get_variables).return_value = variables
    error = live_session_label_change_error(ctx, [], ["live-session"], session_id=SESSION_ID)
    assert (error is None) is authorized
    if not authorized:
        assert error is not None
        assert "get_skill_file" in error
        assert "references/tasks/live-work.md" in error

    automated = _context(agent_run_id="run-1")
    assert automated.session_var_manager is not None
    cast(MagicMock, automated.session_var_manager.get_variables).return_value = variables
    error = live_session_label_change_error(automated, [], ["live-session"], session_id=SESSION_ID)
    assert error is not None
    assert "Spawned and automated" in error
