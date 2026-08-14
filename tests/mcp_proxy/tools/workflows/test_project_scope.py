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
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.pipeline_loader import PipelineLoader
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.integration

PROJECT_ID = "11111111-1111-4111-8111-111111110001"
OTHER_PROJECT_ID = "33333333-3333-4333-8333-333333330003"
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
        (
            SESSION_ID,
            "scoped-session",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            PROJECT_ID,
            "active",
        ),
    )


@pytest.mark.asyncio
async def test_project_scoped_workflow_is_retrievable_from_context(
    temp_db: HubDatabase,
) -> None:
    _create_project(temp_db)
    PipelineDefinitionManager(temp_db).create(
        name="scoped-workflow",
        project_id=PROJECT_ID,
        definition_json={
            "name": "scoped-workflow",
            "type": "pipeline",
            "version": "1.0.0",
            "steps": [{"id": "work", "exec": "echo work"}],
        },
    )
    registry = create_workflows_registry(db=temp_db, loader=PipelineLoader(db=temp_db))

    with _project_tool_context():
        result = await registry.call("get_workflow", {"name": "scoped-workflow"})

    assert result["success"] is True
    assert result["name"] == "scoped-workflow"


@pytest.mark.asyncio
async def test_project_scoped_pipeline_is_retrievable_and_runnable_from_context(
    temp_db: HubDatabase,
) -> None:
    _create_project(temp_db)
    PipelineDefinitionManager(temp_db).create(
        name="scoped-pipeline",
        project_id=PROJECT_ID,
        definition_json={
            "name": "scoped-pipeline",
            "type": "pipeline",
            "version": "1.0.0",
            "steps": [{"id": "work", "exec": "echo scoped"}],
        },
    )
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    execute = AsyncMock(return_value=None)
    executor = SimpleNamespace(execution_manager=execution_manager, execute=execute)
    registry = create_workflows_registry(
        db=temp_db,
        loader=PipelineLoader(db=temp_db),
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


@pytest.mark.asyncio
async def test_project_scoped_pipeline_is_listed_and_shadows_global(
    temp_db: HubDatabase,
) -> None:
    _create_project(temp_db)
    manager = PipelineDefinitionManager(temp_db)
    manager.create(
        name="scoped-pipeline",
        definition_json={
            "name": "scoped-pipeline",
            "type": "pipeline",
            "description": "global pipeline",
            "steps": [{"id": "global", "exec": "echo global"}],
        },
    )
    project_row = manager.create(
        name="scoped-pipeline",
        project_id=PROJECT_ID,
        definition_json={
            "name": "scoped-pipeline",
            "type": "pipeline",
            "description": "project pipeline",
            "steps": [{"id": "project", "exec": "echo project"}],
        },
    )
    registry = create_workflows_registry(db=temp_db, loader=PipelineLoader(db=temp_db))

    with _project_tool_context():
        result = await registry.call("list_pipelines", {})

    assert result["success"] is True
    assert result["count"] == 1
    pipeline = result["pipelines"][0]
    assert pipeline["name"] == "scoped-pipeline"
    assert pipeline["description"] == "project pipeline"
    assert pipeline["is_project"] is True
    assert pipeline["path"].endswith(project_row.id)
    assert pipeline["priority"] == 100
    assert pipeline["step_count"] == 1


@pytest.mark.asyncio
async def test_list_workflows_returns_only_visible_workflow_kinds(
    temp_db: HubDatabase,
) -> None:
    _create_project(temp_db)
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (OTHER_PROJECT_ID, "Other Project"),
    )
    definitions = LocalWorkflowDefinitionManager(temp_db)

    def create_definition(
        name: str,
        *,
        workflow_type: str = "workflow",
        definition_type: str = "step",
        project_id: str | None = PROJECT_ID,
    ) -> None:
        definitions.create(
            name=name,
            workflow_type=workflow_type,
            project_id=project_id,
            definition_json=json.dumps({"name": name, "type": definition_type, "version": "1.0.0"}),
        )

    create_definition("global-step", project_id=None)
    create_definition("shadowed", project_id=None)
    create_definition("shadowed", definition_type="lifecycle")
    create_definition("project-lifecycle", definition_type="lifecycle")
    create_definition("other-project-step", project_id=OTHER_PROJECT_ID)
    for definition_type in ("agent", "pipeline", "rule", "variable"):
        create_definition(
            f"project-{definition_type}",
            workflow_type=definition_type,
            definition_type=definition_type,
        )

    loader = PipelineLoader(db=temp_db)
    loader.global_dirs = []
    registry = create_workflows_registry(db=temp_db, loader=loader)

    with _project_tool_context():
        all_workflows = await registry.call("list_workflows", {})
        lifecycle_workflows = await registry.call("list_workflows", {"workflow_type": "lifecycle"})
        global_workflows = await registry.call("list_workflows", {"global_only": True})
        invalid_filter = await registry.call("list_workflows", {"workflow_type": "pipeline"})

    assert {item["name"] for item in all_workflows["workflows"]} == {
        "global-step",
        "project-lifecycle",
        "shadowed",
    }
    shadowed = next(item for item in all_workflows["workflows"] if item["name"] == "shadowed")
    assert shadowed["type"] == "lifecycle"
    assert {item["name"] for item in lifecycle_workflows["workflows"]} == {
        "project-lifecycle",
        "shadowed",
    }
    assert {item["name"] for item in global_workflows["workflows"]} == {
        "global-step",
        "shadowed",
    }
    assert invalid_filter == {
        "success": False,
        "error": "workflow_type must be 'step' or 'lifecycle'",
    }
