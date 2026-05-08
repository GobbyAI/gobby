"""Helpers for bundled interactive workflow contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_workflow(relative_path: str) -> dict[str, Any]:
    workflow_path = REPO_ROOT / relative_path
    assert workflow_path.exists(), f"{workflow_path} should exist"
    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def workflow_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def has_spawn_agent_step(workflow: dict[str, Any], agent: str) -> bool:
    for item in _walk(workflow):
        if not isinstance(item, dict):
            continue
        if item.get("server") != "gobby-agents" or item.get("tool") != "spawn_agent":
            continue
        arguments = item.get("arguments")
        return isinstance(arguments, dict) and arguments.get("agent") == agent
    return False


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        for child in value.values():
            items.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk(child))
    return items
