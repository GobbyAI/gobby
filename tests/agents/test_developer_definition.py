"""Phase 2 contract tests for the active developer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


def test_developer_yaml_exists_at_active_root() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/developer.yaml"
    )

    assert path.exists()
    agent = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert agent["name"] == "developer"
    assert agent["enabled"] is True
    assert "spawn" in agent["surfaces"]
    assert any(step["name"] == "implement" for step in agent["steps"])
    AgentDefinitionBody.model_validate(agent)


def test_deprecated_developer_tombstone_left_in_place() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/deprecated/developer.yaml"
    )

    assert path.exists()
    agent = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert agent["name"] == "developer"
    assert agent["enabled"] is False
    assert agent["deprecated"] is True
    AgentDefinitionBody.model_validate(agent)
