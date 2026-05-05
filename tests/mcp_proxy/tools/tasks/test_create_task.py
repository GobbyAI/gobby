"""MCP creation surface contracts for Phase 5 task types."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests.storage.tasks._stage_test_helpers import stage_row

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
