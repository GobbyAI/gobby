"""Red tests for the interactive /gobby dev workflow contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / "src/gobby/install/shared/workflows/dev.yaml"


def _load_workflow() -> dict[str, Any]:
    assert WORKFLOW_PATH.exists(), f"{WORKFLOW_PATH} should exist"
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        for child in value.values():
            items.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk(child))
    return items


def _has_spawn_agent_step(workflow: dict[str, Any], agent: str) -> bool:
    for item in _walk(workflow):
        if not isinstance(item, dict):
            continue
        if item.get("server") == "gobby-agents" and item.get("tool") == "spawn_agent":
            arguments = item.get("arguments")
            return isinstance(arguments, dict) and arguments.get("agent") == agent
    return False


def test_developer_agent_wired() -> None:
    workflow = _load_workflow()

    assert workflow["name"] == "dev"
    assert _has_spawn_agent_step(workflow, "developer")
    assert "task_id" in workflow.get("inputs", {})
