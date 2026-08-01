"""Tests for retired bundled workflow and agent definitions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.sync_pipelines import sync_bundled_pipelines

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / "src/gobby/install/shared/workflows"
PIPELINES_DIR = WORKFLOWS_DIR / "pipelines"
AGENTS_DIR = WORKFLOWS_DIR / "agents"
RULES_DIR = WORKFLOWS_DIR / "rules"
PROMPTS_DIR = REPO_ROOT / "src/gobby/install/shared/prompts"

RETIRED_PIPELINES = (
    "orchestrator",
    "front-half-orchestrator",
    "dev-orchestrator",
    "delivery-orchestrator",
    "dev",
    "merge-clone",
    "merge-worktree",
    "nightly-fixes",
    "qa",
    "spawn-developer",
    "spawn-qa",
    "wiki-research",
)
RETIRED_AGENTS = ("developer", "pipeline-worker", "nightly-linter", "nightly-test-fixer")
RETIRED_RULES = {
    "block-and-teach-context7",
    "block-writes-outside-plan-artifact",
    "no-destructive-git-interactive",
    "no-npx",
    "require-memory-review-before-status",
}
MONOLITH_RULES = {
    "require-decompose-monolith-before-threshold-write",
    "require-monolith-resolution-before-commit",
    "require-monolith-resolution-before-task-transition",
    "require-monolith-resolution-before-turn-end",
}


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
    candidates = (
        WORKFLOWS_DIR / f"{name}.yaml",
        PIPELINES_DIR / f"{name}.yaml",
        PIPELINES_DIR / "deprecated" / f"{name}.yaml",
    )

    assert not any(path.exists() for path in candidates), (
        f"retired pipeline remains bundled: {[path for path in candidates if path.exists()]}"
    )


@pytest.mark.parametrize("name", RETIRED_AGENTS)
def test_retired_agent_yaml_is_absent_from_active_and_deprecated_bundles(name: str) -> None:
    active_path = AGENTS_DIR / f"{name}.yaml"
    deprecated_path = AGENTS_DIR / "deprecated" / f"{name}.yaml"

    assert not active_path.exists(), f"retired agent remains active: {active_path}"
    assert not deprecated_path.exists(), f"retired agent tombstone remains: {deprecated_path}"


def test_retired_rules_are_absent_from_bundled_templates() -> None:
    bundled_rule_names: set[str] = set()
    for path in RULES_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("rules"), dict):
            bundled_rule_names.update(data["rules"])

    assert RETIRED_RULES.isdisjoint(bundled_rule_names)


def test_monolith_rule_templates_match_enabled_db_authority() -> None:
    path = RULES_DIR / "monolith-enforcement/require-same-session-decomposition.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert set(data["rules"]) == MONOLITH_RULES
    assert all(rule["enabled"] is True for rule in data["rules"].values())


def test_bundled_agents_have_no_dead_sync_selector() -> None:
    offenders: list[str] = []
    for path in AGENTS_DIR.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        excludes = data.get("workflows", {}).get("rule_selectors", {}).get("exclude", [])
        if "tag:sync" in excludes:
            offenders.append(path.name)

    assert offenders == []


def test_retired_digest_prompt_is_absent() -> None:
    assert not (PROMPTS_DIR / "memory/digest_update.md").exists()


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

    assert manager.get_by_name("orchestrator") is None
    row = manager.get_by_name("orchestrator", include_deleted=True)
    assert row is not None
    assert row.deleted_at is not None
    assert row.enabled is True
    assert "deprecated" not in json.loads(row.definition_json)


@pytest.mark.parametrize("name", RETIRED_AGENTS)
def test_removed_bundled_agent_sync_soft_deletes_installed_row(
    name: str, tmp_path: Path, temp_db: HubDatabase
) -> None:
    db = temp_db
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name=name,
        workflow_type="agent",
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
    definition = json.loads(row.definition_json)
    assert definition == {
        "name": name,
        "description": "old definition",
        "enabled": True,
    }
