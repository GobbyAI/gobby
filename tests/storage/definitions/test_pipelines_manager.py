"""CRUD, duplicate, scope-move, and canvas/version tests for pipelines."""

from __future__ import annotations

from uuid import uuid4

import pytest

from gobby.storage.definitions import (
    DefinitionNameConflictError,
    PipelineDefinitionManager,
)
from gobby.storage.hub.postgres import PostgresHubDatabase

_PROJECT = str(uuid4())


def _mgr(db: PostgresHubDatabase) -> PipelineDefinitionManager:
    return PipelineDefinitionManager(db)


def test_crud_duplicate_scope_and_canvas_version(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    created = manager.create(
        name="lint",
        definition_json={"steps": [{"run": "ruff"}]},
        version="1.0",
        canvas_json={"x": 1},
        tags=["ci"],
    )
    assert created.version == "1.0"
    assert created.canvas_json == {"x": 1}

    updated = manager.update(
        created.id,
        version="2.0",
        canvas_json={"x": 2},
        description="lint pipeline",
    )
    assert updated.version == "2.0"
    assert updated.canvas_json == {"x": 2}
    assert updated.description == "lint pipeline"

    moved = manager.move_to_project(updated.id, _PROJECT)
    assert moved.project_id == _PROJECT
    copy = manager.duplicate(moved.id, "lint-copy")
    assert copy.name == "lint-copy"
    assert copy.project_id == _PROJECT
    assert copy.canvas_json == {"x": 2}
    with pytest.raises(DefinitionNameConflictError):
        manager.duplicate(moved.id, "lint")
    globalized = manager.move_to_global(moved.id)
    assert globalized.project_id is None


def test_pipeline_live_conflict_and_restore(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    first = manager.create(name="build", definition_json={"steps": []})
    with pytest.raises(DefinitionNameConflictError):
        manager.create(name="build", definition_json={"steps": [{"run": "x"}]})
    manager.delete(first.id)
    replacement = manager.create(name="build", definition_json={"steps": [{"run": "y"}]})
    with pytest.raises(DefinitionNameConflictError):
        manager.restore(first.id)
    manager.hard_delete(replacement.id)
    restored = manager.restore(first.id)
    assert restored.definition_json == {"steps": []}
