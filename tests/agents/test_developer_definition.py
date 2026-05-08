"""Contract tests for the retired developer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


def test_developer_yaml_is_not_active() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/developer.yaml"
    )

    assert not path.exists()


def test_deprecated_developer_definition_left_in_place() -> None:
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
