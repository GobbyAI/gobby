"""Integration coverage for project-scoped workflow and pipeline tools."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gobby.mcp_proxy.tools.workflows import create_workflows_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.loader import WorkflowLoader
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.integration

PROJECT_ID = "11111111-1111-4111-8111-111111110001"
SESSION_ID = "22222222-2222-4222-8222-222222220002"


@contextmanager
def _project_tool_context() -> Iterator[None]:
    token = set_project_context(
        {
            "id": PROJECT_ID,
            "name": "Scoped Project",
            "path": "/tmp/scoped-project",
        }
    )
    try:
        with session_context_for_test(SESSION_ID):
            yield
    finally:
        reset_project_context(token)


def _create_project(db: HubDatabase) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (PROJECT_ID, "Scoped Project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, status, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (SESSION_ID, "scoped-session", "machine-1", "codex", PROJECT_ID, "active"),
    )


@pytest.mark.asyncio
async def test_project_scoped_workflow_is_retrievable_from_context(
    temp_db: HubDatabase,
) -> None:
    _create_project(temp_db)
    definitions = LocalWorkflowDefinitionManager(temp_db)
    definitions.create(
        name="scoped-workflow",
        workflow_type="workflow",
        project_id=PROJECT_ID,
        definition_json=json.dumps(
            {
                "name": "scoped-workflow",
                "version": "1.0.0",
                "steps": [{"name": "work", "allowed_tools": "all"}],
            }
        ),
    )
    registry = create_workflows_registry(db=temp_db, loader=WorkflowLoader(db=temp_db))

    with _project_tool_context():
        result = await registry.call("get_workflow", {"name": "scoped-workflow"})

    assert result["success"] is True
    assert result["name"] == "scoped-workflow"


@pytest.mark.asyncio
async def test_project_scoped_pipeline_is_retrievable_and_runnable_from_context(
    temp_db: HubDatabase,
) -> None:
    _create_project(temp_db)
    definitions = LocalWorkflowDefinitionManager(temp_db)
    definitions.create(
        name="scoped-pipeline",
        workflow_type="pipeline",
        project_id=PROJECT_ID,
        definition_json=json.dumps(
            {
                "name": "scoped-pipeline",
                "type": "pipeline",
                "version": "1.0.0",
                "steps": [{"id": "work", "exec": "echo scoped"}],
            }
        ),
    )
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    execute = AsyncMock(return_value=None)
    executor = SimpleNamespace(execution_manager=execution_manager, execute=execute)
    registry = create_workflows_registry(
        db=temp_db,
        loader=WorkflowLoader(db=temp_db),
        executor_getter=lambda: executor,
        execution_manager_getter=lambda: execution_manager,
    )

    with _project_tool_context():
        fetched = await registry.call("get_pipeline", {"name": "scoped-pipeline"})
        started = await registry.call("run_pipeline", {"name": "scoped-pipeline", "inputs": {}})
        await drain_asyncio_tasks(cycles=2)

    assert fetched["success"] is True
    assert fetched["name"] == "scoped-pipeline"
    assert started["success"] is True, started
    assert started["status"] == "running"
    execution = execution_manager.get_execution(started["execution_id"])
    assert execution is not None
    assert execution.project_id == PROJECT_ID
    execute.assert_awaited_once()
