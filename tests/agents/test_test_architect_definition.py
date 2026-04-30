"""Phase 2 contract tests for the test-architect agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_definition_loads() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/test-architect.yaml"
    )
    agent = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert agent["name"] == "test-architect"
    assert "spawn" in agent["surfaces"]
    assert any(step["name"] == "design" for step in agent["steps"])
