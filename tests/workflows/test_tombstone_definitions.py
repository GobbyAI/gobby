"""Tests for retired workflow and agent tombstone definitions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody, PipelineDefinition
from gobby.workflows.sync_pipelines import sync_bundled_pipelines

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINES_DIR = REPO_ROOT / "src/gobby/install/shared/workflows/pipelines"
AGENTS_DIR = REPO_ROOT / "src/gobby/install/shared/workflows/agents"

RETIRED_PIPELINES = (
    "orchestrator",
    "front-half-orchestrator",
    "conductor",
    "dev-orchestrator",
    "delivery-orchestrator",
)
RETIRED_AGENTS = ("conductor", "developer")


def _load_yaml(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _make_db(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "test.db")
    run_migrations(db)
    return db


def test_no_external_conductor_imports_remain() -> None:
    module = "gobby" + ".conductor"
    matches: list[str] = []

    for base in (REPO_ROOT / "src", REPO_ROOT / "tests"):
        for path in base.rglob("*.py"):
            text = path.read_text()
            if f"from {module}" in text or f"import {module}" in text:
                matches.append(str(path.relative_to(REPO_ROOT)))

    assert matches == []


@pytest.mark.parametrize("name", RETIRED_PIPELINES)
def test_retired_pipeline_yaml_is_disabled_tombstone(name: str) -> None:
    path = PIPELINES_DIR / f"{name}.yaml"
    assert path.exists(), f"missing tombstone pipeline file: {path}"

    data = _load_yaml(path)

    assert data["name"] == name
    assert data["version"] == "2.1"
    assert data["enabled"] is False
    assert data["deprecated"] is True
    assert isinstance(data["deprecated_reason"], str)
    assert "state-driven dispatcher" in data["deprecated_reason"]
    assert str(data["description"]).startswith("[DEPRECATED]")
    assert data["steps"] == []

    definition = PipelineDefinition.model_validate(data)
    assert definition.deprecated is True
    assert definition.deprecated_reason == data["deprecated_reason"]


@pytest.mark.parametrize("name", RETIRED_AGENTS)
def test_retired_agent_yaml_is_disabled_tombstone(name: str) -> None:
    path = AGENTS_DIR / f"{name}.yaml"
    assert path.exists(), f"missing tombstone agent file: {path}"

    data = _load_yaml(path)

    assert data["name"] == name
    assert data["enabled"] is False
    assert data["deprecated"] is True
    assert isinstance(data["deprecated_reason"], str)
    assert data["deprecated_reason"]

    body = AgentDefinitionBody.model_validate(data)
    assert body.deprecated is True
    assert body.deprecated_reason == data["deprecated_reason"]


def test_tombstoned_pipeline_sync_preserves_installed_row(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="orchestrator",
        workflow_type="pipeline",
        definition_json=json.dumps(
            {
                "name": "orchestrator",
                "type": "pipeline",
                "description": "old definition",
                "steps": [{"id": "noop", "exec": "true"}],
            }
        ),
        source="installed",
        tags=["gobby"],
        enabled=True,
    )

    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "orchestrator.yaml").write_text(
        """
name: orchestrator
type: pipeline
version: "2.1"
enabled: false
deprecated: true
deprecated_reason: "Replaced by the state-driven dispatcher."
description: "[DEPRECATED] Replaced by the state-driven dispatcher."
steps: []
"""
    )

    with patch(
        "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pipelines_dir
    ):
        result = sync_bundled_pipelines(db)

    assert result["errors"] == []
    assert result["orphaned"] == 0

    row = manager.get_by_name("orchestrator")
    assert row is not None
    assert row.deleted_at is None
    assert row.enabled is True
    assert json.loads(row.definition_json)["deprecated"] is True


def test_tombstoned_agent_sync_preserves_installed_row_and_metadata(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="developer",
        workflow_type="agent",
        definition_json=json.dumps(
            {
                "name": "developer",
                "description": "old definition",
                "enabled": True,
            }
        ),
        source="installed",
        tags=["gobby"],
        enabled=True,
    )

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "developer.yaml").write_text(
        """
name: developer
description: "[DEPRECATED] Replaced by frontend-developer and backend-developer."
enabled: false
deprecated: true
deprecated_reason: "Replaced by frontend-developer and backend-developer."
"""
    )

    with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
        result = sync_bundled_agents(db)

    assert result["errors"] == []
    assert result["orphaned"] == 0

    row = manager.get_by_name("developer")
    assert row is not None
    assert row.deleted_at is None
    assert row.enabled is True
    definition = json.loads(row.definition_json)
    assert definition["deprecated"] is True
    assert (
        definition["deprecated_reason"] == "Replaced by frontend-developer and backend-developer."
    )
