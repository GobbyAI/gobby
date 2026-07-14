"""MCP creation surface contracts for Phase 5 task types."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._crud import build_task_tree, create_crud_registry
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests.storage.tasks._stage_test_helpers import stage_row


@pytest.mark.asyncio
async def test_create_task_fails_closed_when_session_project_lookup_errors(
    temp_db,
    sample_project,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fallback_project = LocalProjectManager(temp_db).create(
        "task-create-fallback",
        repo_path="/tmp/task-create-fallback",
    )
    session = SessionManager(temp_db).register(
        external_id="task-create-project-error",
        machine_id="test-machine",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager, sync_manager=MagicMock())
    registry = create_crud_registry(ctx)
    session_count = manager.count_tasks(project_id=sample_project["id"])
    fallback_count = manager.count_tasks(project_id=fallback_project.id)
    ctx.session_manager.get = MagicMock(side_effect=RuntimeError("session database unavailable"))

    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._context.get_project_context",
            return_value={"id": fallback_project.id},
        ),
        caplog.at_level(logging.WARNING, logger="gobby.mcp_proxy.tools.tasks._crud"),
        session_context_for_test(session.id),
    ):
        result = await registry.call(
            "create_task",
            {"title": "Must not cross projects", "category": "test"},
        )

    assert "session database unavailable" in result["error"]
    ctx.session_manager.get.assert_called_once_with(session.id)
    assert manager.count_tasks(project_id=sample_project["id"]) == session_count
    assert manager.count_tasks(project_id=fallback_project.id) == fallback_count
    assert "Cannot resolve project" in caplog.text


def test_build_task_tree_fails_closed_when_session_project_lookup_errors(
    temp_db,
    sample_project,
) -> None:
    session = SessionManager(temp_db).register(
        external_id="task-tree-project-error",
        machine_id="test-machine",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager, sync_manager=MagicMock())
    before_count = manager.count_tasks(project_id=sample_project["id"])
    ctx.session_manager.get = MagicMock(side_effect=RuntimeError("session database unavailable"))

    result = build_task_tree(
        ctx,
        {"title": "Must not build", "task_type": "epic", "children": []},
        session.id,
    )

    assert result["success"] is False
    assert result["tasks_created"] == 0
    assert "session database unavailable" in result["error"]
    assert manager.count_tasks(project_id=sample_project["id"]) == before_count


pytestmark = pytest.mark.unit


def test_simple_fix_type(task_registry) -> None:
    schema = task_registry.get_schema("create_task")
    task_type_schema = schema["inputSchema"]["properties"]["task_type"]

    assert "simple_fix" in task_type_schema.get("enum", [])
    assert "review_anchor" in task_type_schema.get("enum", [])


@pytest.mark.asyncio
async def test_create_task_does_not_accept_or_seed_stage_caps(temp_db, sample_project) -> None:
    session = SessionManager(temp_db).register(
        external_id="stage-caps-mcp",
        machine_id="test-machine",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    schema = registry.get_schema("create_task")

    assert "stage_caps" not in schema["inputSchema"]["properties"]

    with session_context_for_test(session.id):
        result = await registry.call(
            "create_task",
            {
                "title": "Metadata-only review anchor",
                "category": "planning",
                "task_type": "review_anchor",
            },
        )

    assert "error" not in result
    assert manager.stage_states.list_for_task(result["id"]) == []


@pytest.mark.asyncio
async def test_create_and_update_task_round_trip_schedule_fields(temp_db, sample_project) -> None:
    session = SessionManager(temp_db).register(
        external_id="schedule-fields-mcp",
        machine_id="test-machine",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())

    with session_context_for_test(session.id):
        created = await registry.call(
            "create_task",
            {
                "title": "Scheduled task",
                "category": "research",
                "start_date": "2026-07-14",
                "due_date": "2026-07-21",
            },
        )
        update_result = await registry.call(
            "update_task",
            {
                "task_id": created["id"],
                "start_date": "2026-07-15",
                "due_date": "2026-07-22",
            },
        )

    assert "error" not in created
    assert update_result == {}
    updated = manager.get_task(created["id"])
    assert updated.start_date == "2026-07-15"
    assert updated.due_date == "2026-07-22"


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name,value", [("verification", "Run tests"), ("sequence_order", 1)])
async def test_update_task_reports_unsupported_fields(
    temp_db, sample_project, field_name, value
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Unsupported update")
    registry = create_task_registry(manager, MagicMock())

    result = await registry.call(
        "update_task",
        {"task_id": task.id, field_name: value},
    )

    assert result == {
        "error": f"LocalTaskManager.update_task received unsupported fields: {field_name}"
    }


@pytest.mark.asyncio
async def test_initialize_task_manifest_persists_review_anchor_cap(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Capped review anchor",
        category="planning",
        task_type="review_anchor",
    )
    ctx = RegistryContext(task_manager=manager, sync_manager=MagicMock())
    registry = create_stage_ops_registry(ctx)
    schema = registry.get_schema("initialize_task_manifest")

    assert "stage_caps" in schema["inputSchema"]["properties"]

    result = await registry.call(
        "initialize_task_manifest",
        {
            "task_id": task.id,
            "stage_names": ["planning"],
            "stage_caps": [{"stage_name": "planning", "max_review_rounds": 100}],
        },
    )

    assert result["ok"] is True
    assert result["stages"][0]["display_label"] == "Planning"
    planning = stage_row(temp_db, result["task_id"], "planning")
    assert planning["max_review_rounds"] == 100


@pytest.mark.asyncio
async def test_initialize_task_manifest_rejects_unknown_stage(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Bad stage task",
        category="planning",
        task_type="review_anchor",
    )
    registry = create_stage_ops_registry(
        RegistryContext(task_manager=manager, sync_manager=MagicMock())
    )

    with pytest.raises(ValueError, match="Unknown stage 'missing'"):
        await registry.call(
            "initialize_task_manifest",
            {"task_id": task.id, "stage_names": ["planning", "missing"]},
        )
