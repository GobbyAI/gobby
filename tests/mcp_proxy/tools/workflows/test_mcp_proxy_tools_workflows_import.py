"""Round-trip coverage for workflow imports into the runtime database."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gobby.mcp_proxy.tools.workflows import create_workflows_registry
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.pipeline_loader import PipelineLoader
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.integration

PROJECT_ID = "11111111-1111-4111-8111-111111110001"
SESSION_ID = "22222222-2222-4222-8222-222222220002"


def _create_project(db: HubDatabase, project_path: Path) -> None:
    (project_path / ".gobby").mkdir()
    (project_path / ".gobby" / "project.json").write_text(
        json.dumps({"id": PROJECT_ID, "name": "Import Project"}),
        encoding="utf-8",
    )
    db.execute(
        "INSERT INTO projects (id, name, repo_path, created_at, updated_at) "
        "VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (PROJECT_ID, "Import Project", str(project_path)),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, status, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            SESSION_ID,
            "import-session",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            PROJECT_ID,
            "active",
        ),
    )


@contextmanager
def _tool_context(project_path: Path) -> Iterator[None]:
    token = set_project_context(
        {
            "id": PROJECT_ID,
            "name": "Import Project",
            "project_path": str(project_path),
        }
    )
    try:
        with session_context_for_test(SESSION_ID):
            yield
    finally:
        reset_project_context(token)


@pytest.mark.asyncio
async def test_imported_step_workflow_is_immediately_gettable_and_updatable(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    _create_project(temp_db, project_path)
    source = tmp_path / "source-step.yaml"
    source.write_text(
        """name: imported-step
type: pipeline
version: 1.0
description: First version
steps:
  - id: work
    exec: echo work
""",
        encoding="utf-8",
    )
    loader = PipelineLoader(db=temp_db)
    loader.global_dirs = []
    registry = create_workflows_registry(db=temp_db, loader=loader)

    yaml_v1 = source.read_text(encoding="utf-8")
    with _tool_context(project_path):
        imported = await registry.call(
            "create_pipeline",
            {"yaml_content": yaml_v1, "project_id": PROJECT_ID},
        )
        fetched = await registry.call("get_pipeline", {"name": "imported-step"})
        loaded_by_path = await loader.load_pipeline("imported-step", project_path=project_path)
        discovered_by_path = await loader.discover_pipelines(project_path=project_path)

        yaml_v2 = yaml_v1.replace("First version", "Second version")
        updated = await registry.call(
            "update_pipeline",
            {
                "definition_id": imported["definition"]["id"],
                "yaml_content": yaml_v2,
            },
        )
        fetched_updated = await registry.call("get_pipeline", {"name": "imported-step"})

    assert imported["success"] is True
    imported_id = imported["definition"]["id"]
    assert updated["success"] is True
    assert updated["definition"]["id"] == imported_id
    assert fetched["success"] is True
    assert fetched["description"] == "First version"
    assert loaded_by_path is not None
    assert loaded_by_path.description == "First version"
    assert any(item.name == "imported-step" and item.is_project for item in discovered_by_path)
    assert fetched_updated["description"] == "Second version"
    row = PipelineDefinitionManager(temp_db).get(imported_id)
    assert row.project_id == PROJECT_ID
    assert row.enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled_yaml", "expected_enabled"),
    [("", True), ('enabled: "false"\n', False)],
)
async def test_imported_pipeline_is_immediately_gettable_and_honors_enabled(
    temp_db: HubDatabase,
    tmp_path: Path,
    enabled_yaml: str,
    expected_enabled: bool,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    _create_project(temp_db, project_path)
    source = tmp_path / "source-pipeline.yaml"
    source.write_text(
        f"""name: imported-pipeline
type: pipeline
version: 1.0
{enabled_yaml}steps:
  - id: work
    exec: echo imported
""",
        encoding="utf-8",
    )
    loader = PipelineLoader(db=temp_db)
    loader.global_dirs = []
    execution_manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    execute = AsyncMock(return_value=None)
    executor = SimpleNamespace(execution_manager=execution_manager, execute=execute)
    registry = create_workflows_registry(
        db=temp_db,
        loader=loader,
        executor_getter=lambda: executor,
        execution_manager_getter=lambda: execution_manager,
    )

    with _tool_context(project_path):
        imported = await registry.call(
            "create_pipeline",
            {"yaml_content": source.read_text(encoding="utf-8"), "project_id": PROJECT_ID},
        )
        fetched = await registry.call("get_pipeline", {"name": "imported-pipeline"})
        loaded_by_path = await loader.load_pipeline("imported-pipeline", project_path=project_path)
        discovered_by_path = await loader.discover_pipelines(project_path=project_path)
        started = await registry.call("run_pipeline", {"name": "imported-pipeline", "inputs": {}})
        await drain_asyncio_tasks(cycles=2)

    assert imported["success"] is True
    assert fetched["success"] is True
    assert fetched["enabled"] is expected_enabled
    assert loaded_by_path is not None
    assert loaded_by_path.enabled is expected_enabled
    assert (
        any(item.name == "imported-pipeline" and item.is_project for item in discovered_by_path)
        is expected_enabled
    )
    row = PipelineDefinitionManager(temp_db).get(imported["definition"]["id"])
    assert row.enabled is expected_enabled
    if expected_enabled:
        assert started["success"] is True
        execution = execution_manager.get_execution(started["execution_id"])
        assert execution is not None
        assert execution.project_id == PROJECT_ID
        execute.assert_awaited_once()
    else:
        assert started["success"] is False
        assert "disabled" in started["error"]
        execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_import_is_immediately_gettable(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    _create_project(temp_db, project_path)
    source = tmp_path / "global-step.yaml"
    source.write_text(
        """name: global-step
type: pipeline
enabled: false
steps:
  - id: work
    exec: echo work
""",
        encoding="utf-8",
    )
    loader = PipelineLoader(db=temp_db)
    loader.global_dirs = []
    registry = create_workflows_registry(db=temp_db, loader=loader)

    with _tool_context(project_path):
        imported = await registry.call(
            "create_pipeline",
            {"yaml_content": source.read_text(encoding="utf-8")},
        )
        fetched = await registry.call("get_pipeline", {"name": "global-step"})

    assert imported["success"] is True
    assert fetched["success"] is True
    row = PipelineDefinitionManager(temp_db).get(imported["definition"]["id"])
    assert row.project_id is None
    assert row.enabled is False
