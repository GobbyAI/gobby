"""MCP creation surface contracts for Phase 5 task types."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
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
async def test_create_task_accepts_stage_caps_and_persists_manifest_cap(
    temp_db, sample_project
) -> None:
    session = SessionManager(temp_db).register(
        external_id="stage-caps-mcp",
        machine_id="test-machine",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    schema = registry.get_schema("create_task")

    assert "stage_caps" in schema["inputSchema"]["properties"]

    with session_context_for_test(session.id):
        result = await registry.call(
            "create_task",
            {
                "title": "Capped review anchor",
                "category": "planning",
                "task_type": "review_anchor",
                "stage_caps": [
                    {"stage_name": "planning", "max_review_rounds": 100},
                ],
            },
        )

    assert "error" not in result
    planning = stage_row(temp_db, result["id"], "planning")
    assert planning["max_review_rounds"] == 100
