"""Synthetic installed artifacts are retired by source discovery."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


def test_removed_bundled_pipeline_sync_soft_deletes_installed_row(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    db = temp_db
    manager = PipelineDefinitionManager(db)
    manager.create(
        name="removed-pipeline",
        definition_json={
            "name": "removed-pipeline",
            "type": "pipeline",
            "description": "old definition",
            "steps": [{"id": "noop", "exec": "true"}],
        },
        source="installed",
        tags=["gobby"],
        enabled=True,
    )

    pipelines_dir = tmp_path / "pipelines"
    pipelines_dir.mkdir()
    (pipelines_dir / "retained.yaml").write_text(
        """
name: retained
type: pipeline
description: retained definition
enabled: false
steps:
  - id: noop
    exec: "true"
""",
        encoding="utf-8",
    )

    with patch(
        "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pipelines_dir
    ):
        result = sync_bundled_pipelines(db)

    assert result["errors"] == []
    assert result["orphaned"] == 1

    assert manager.get_by_name("removed-pipeline") is None
    row = manager.get_by_name("removed-pipeline", include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is True
    assert "deprecated" not in json.dumps(row.definition_json)


def test_removed_clean_tree_rule_sync_soft_deletes_installed_row(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    manager = RuleDefinitionManager(temp_db)
    manager.create(
        name="removed-rule",
        definition_json={
            "event": "before_tool",
            "effects": [{"type": "block", "reason": "old clean-tree gate"}],
        },
        source="installed",
        tags=["gobby"],
        enabled=True,
    )
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "retained.yaml").write_text(
        """rules:
  retained-rule:
    event: before_tool
    effect:
      type: block
      reason: retained
""",
        encoding="utf-8",
    )

    result = sync_bundled_rules(temp_db, rules_dir)

    assert result["errors"] == []
    assert result["orphaned"] == 1
    assert manager.get_by_name("removed-rule") is None
    row = manager.get_by_name("removed-rule", include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None


@pytest.mark.parametrize("name", ["removed-seat"])
def test_removed_bundled_agent_sync_soft_deletes_installed_row(
    name: str, tmp_path: Path, temp_db: HubDatabase
) -> None:
    db = temp_db
    manager = AgentDefinitionManager(db)
    manager.create(
        name=name,
        definition_json=json.dumps(
            {
                "name": name,
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

    assert manager.get_by_name(name) is None
    row = manager.get_by_name(name, include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is True
    definition = row.definition_json
    assert definition == {
        "name": name,
        "description": "old definition",
        "enabled": True,
    }
