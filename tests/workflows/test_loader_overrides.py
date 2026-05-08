"""Tests for workflow loader override conflict handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.loader import WorkflowLoader

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "loader-overrides.db")
    run_migrations(database)
    return database


@pytest.fixture
def def_manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _workflow_json(name: str, *, override: bool = False) -> str:
    data: dict[str, object] = {
        "name": name,
        "steps": [{"name": "work", "allowed_tools": "all"}],
    }
    if override:
        data["override"] = True
    return json.dumps(data)


@pytest.mark.asyncio
async def test_conflict_without_override_label_fails_loud(
    db: LocalDatabase,
    def_manager: LocalWorkflowDefinitionManager,
) -> None:
    project = LocalProjectManager(db).create(name="test-project", repo_path="/tmp/test-project")
    def_manager.create(
        name="shared-workflow",
        definition_json=_workflow_json("shared-workflow"),
        workflow_type="workflow",
        tags=["gobby"],
    )
    def_manager.create(
        name="shared-workflow",
        definition_json=_workflow_json("shared-workflow"),
        workflow_type="workflow",
        project_id=project.id,
        source="project",
        tags=["user"],
    )

    loader = WorkflowLoader(db=db)

    with pytest.raises(ValueError, match="override: true"):
        await loader.load_workflow("shared-workflow", project_path=project.id)


@pytest.mark.asyncio
async def test_conflict_with_override_label_loads_project_copy(
    db: LocalDatabase,
    def_manager: LocalWorkflowDefinitionManager,
) -> None:
    project = LocalProjectManager(db).create(name="test-project", repo_path="/tmp/test-project")
    def_manager.create(
        name="shared-workflow",
        definition_json=_workflow_json("shared-workflow"),
        workflow_type="workflow",
        tags=["gobby"],
    )
    def_manager.create(
        name="shared-workflow",
        definition_json=_workflow_json("shared-workflow", override=True),
        workflow_type="workflow",
        project_id=project.id,
        source="project",
        tags=["user"],
    )

    workflow = await WorkflowLoader(db=db).load_workflow("shared-workflow", project_path=project.id)

    assert workflow is not None
    assert workflow.name == "shared-workflow"
