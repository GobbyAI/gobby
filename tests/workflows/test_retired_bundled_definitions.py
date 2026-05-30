"""Tests for retired bundled workflow and agent definitions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.sync_pipelines import sync_bundled_pipelines

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINES_DIR = REPO_ROOT / "src/gobby/install/shared/workflows/pipelines"
AGENTS_DIR = REPO_ROOT / "src/gobby/install/shared/workflows/agents"

RETIRED_PIPELINES = (
    "orchestrator",
    "front-half-orchestrator",
    "dev-orchestrator",
    "delivery-orchestrator",
)
RETIRED_AGENTS = ("developer", "pipeline-worker")


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
def test_retired_pipeline_yaml_is_absent_from_active_and_deprecated_bundles(
    name: str,
) -> None:
    active_path = PIPELINES_DIR / f"{name}.yaml"
    deprecated_path = PIPELINES_DIR / "deprecated" / f"{name}.yaml"

    assert not active_path.exists(), f"retired pipeline remains active: {active_path}"
    assert not deprecated_path.exists(), (
        f"retired pipeline deprecated YAML remains: {deprecated_path}"
    )


@pytest.mark.parametrize("name", RETIRED_AGENTS)
def test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles(name: str) -> None:
    active_path = AGENTS_DIR / f"{name}.yaml"
    deprecated_path = AGENTS_DIR / "deprecated" / f"{name}.yaml"

    assert not active_path.exists(), f"retired agent remains active: {active_path}"
    assert deprecated_path.exists(), f"retired agent tombstone is missing: {deprecated_path}"


def test_removed_bundled_pipeline_sync_soft_deletes_installed_row(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    db = temp_db
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

    with patch(
        "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pipelines_dir
    ):
        result = sync_bundled_pipelines(db)

    assert result["errors"] == []
    assert result["orphaned"] == 1

    assert manager.get_by_name("orchestrator") is None
    row = manager.get_by_name("orchestrator", include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is True
    assert "deprecated" not in json.loads(row.definition_json)


def test_removed_bundled_agent_sync_soft_deletes_installed_row(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    db = temp_db
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

    with patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir):
        result = sync_bundled_agents(db)

    assert result["errors"] == []
    assert result["orphaned"] == 1

    assert manager.get_by_name("developer") is None
    row = manager.get_by_name("developer", include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is True
    definition = json.loads(row.definition_json)
    assert definition == {
        "name": "developer",
        "description": "old definition",
        "enabled": True,
    }
