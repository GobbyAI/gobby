"""Tests for retirement migration of obsolete workflow definitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    _apply_baseline,
    get_current_version,
    run_migrations,
)
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

pytestmark = pytest.mark.unit

RETIRED_PIPELINES = (
    "orchestrator",
    "front-half-orchestrator",
    "conductor",
    "dev-orchestrator",
    "delivery-orchestrator",
)
RETIRED_AGENTS = ("conductor", "developer", "pipeline-worker")


def _make_v220_db(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "retirement.db")
    _apply_baseline(db)
    assert get_current_version(db) == 220
    return db


def _create_workflow_definition(
    manager: LocalWorkflowDefinitionManager,
    *,
    name: str,
    workflow_type: str,
    source: str = "installed",
    tags: list[str] | None = None,
    enabled: bool = True,
) -> None:
    definition: dict[str, object]
    if workflow_type == "pipeline":
        definition = {
            "name": name,
            "type": "pipeline",
            "description": "legacy pipeline",
            "steps": [{"id": "noop", "exec": "true"}],
        }
    else:
        definition = {
            "name": name,
            "description": "legacy agent",
            "enabled": enabled,
        }

    manager.create(
        name=name,
        workflow_type=workflow_type,
        definition_json=json.dumps(definition),
        source=source,
        tags=tags,
        enabled=enabled,
    )


def test_retirement_migration_disables_installed_gobby_pipeline_rows(tmp_path: Path) -> None:
    db = _make_v220_db(tmp_path)
    manager = LocalWorkflowDefinitionManager(db)

    for name in RETIRED_PIPELINES:
        _create_workflow_definition(
            manager,
            name=name,
            workflow_type="pipeline",
            tags=["gobby"],
        )
    _create_workflow_definition(
        manager,
        name="orchestrator-custom",
        workflow_type="pipeline",
        source="custom",
        tags=["user"],
    )

    applied = run_migrations(db)

    expected_applied = sum(1 for version, _d, _a in MIGRATIONS if version > BASELINE_VERSION)
    latest_version = max(version for version, _d, _a in MIGRATIONS)
    assert applied == expected_applied
    assert get_current_version(db) == latest_version

    for name in RETIRED_PIPELINES:
        row = manager.get_by_name(name)
        assert row is not None
        assert row.workflow_type == "pipeline"
        assert row.enabled is False
        assert row.deleted_at is None

    custom = manager.get_by_name("orchestrator-custom")
    assert custom is not None
    assert custom.enabled is True

    assert run_migrations(db) == 0


def test_retirement_migration_disables_installed_gobby_agent_rows(tmp_path: Path) -> None:
    db = _make_v220_db(tmp_path)
    manager = LocalWorkflowDefinitionManager(db)

    for name in RETIRED_AGENTS:
        _create_workflow_definition(
            manager,
            name=name,
            workflow_type="agent",
            tags=["gobby"],
        )
    _create_workflow_definition(
        manager,
        name="developer-custom",
        workflow_type="agent",
        source="custom",
        tags=["user"],
    )

    applied = run_migrations(db)

    expected_applied = sum(1 for version, _d, _a in MIGRATIONS if version > BASELINE_VERSION)
    latest_version = max(version for version, _d, _a in MIGRATIONS)
    assert applied == expected_applied
    assert get_current_version(db) == latest_version

    for name in RETIRED_AGENTS:
        row = manager.get_by_name(name)
        assert row is not None
        assert row.workflow_type == "agent"
        assert row.enabled is False
        assert row.deleted_at is None

    custom = manager.get_by_name("developer-custom")
    assert custom is not None
    assert custom.enabled is True

    assert run_migrations(db) == 0
