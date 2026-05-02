"""Red tests for disabled placeholder agents used by discovery stages."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

AGENTS_DIR = Path("src/gobby/install/shared/workflows/agents")
PLACEHOLDERS = {
    "analyst": "ideation",
    "researcher": "research",
    "architect": "architecture",
    "product-manager": "prd",
}


def _placeholder_path(slug: str) -> Path:
    return AGENTS_DIR / f"{slug}.yaml"


def _placeholder_yaml(slug: str) -> dict:
    path = _placeholder_path(slug)
    assert path.exists(), f"missing placeholder agent YAML: {path}"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _assert_placeholder(slug: str, stage_name: str) -> None:
    path = _placeholder_path(slug)
    text = path.read_text(encoding="utf-8")
    payload = _placeholder_yaml(slug)

    assert "PLACEHOLDER" in text
    assert payload["name"] == slug
    assert payload["enabled"] is False
    assert payload["priority"] == 1
    assert stage_name in payload["description"]
    assert f"placeholder_agent:{slug}:not_implemented" in payload["instructions"]
    AgentDefinitionBody.model_validate(payload)


def _fresh_db(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "placeholder-agents.db")
    run_migrations(db)
    return db


def test_each_placeholder_escalates_on_spawn() -> None:
    for slug, stage_name in PLACEHOLDERS.items():
        _assert_placeholder(slug, stage_name)


def test_sync_installs_disabled(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)

    result = sync_bundled_agents(db)

    assert result["errors"] == []
    manager = LocalWorkflowDefinitionManager(db)
    for slug in PLACEHOLDERS:
        row = manager.get_by_name(slug)
        assert row is not None
        assert row.workflow_type == "agent"
        assert row.source == "installed"
        assert row.enabled is False


def test_analyst_placeholder_for_ideation_stage() -> None:
    _assert_placeholder("analyst", "ideation")


def test_researcher_placeholder_for_research_stage() -> None:
    _assert_placeholder("researcher", "research")


def test_architect_placeholder_for_architecture_stage() -> None:
    _assert_placeholder("architect", "architecture")


def test_product_manager_placeholder_for_prd_stage() -> None:
    _assert_placeholder("product-manager", "prd")
