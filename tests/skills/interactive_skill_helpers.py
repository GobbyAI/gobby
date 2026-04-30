"""Helpers for bundled interactive skill contract tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_skill(relative_path: str) -> str:
    skill_path = REPO_ROOT / relative_path
    assert skill_path.exists(), f"{skill_path} should exist"
    return skill_path.read_text(encoding="utf-8")


def assert_interactive_skill_contract(
    body: str,
    *,
    name: str,
    command: str,
    agent: str,
    workflow_path: str,
) -> None:
    frontmatter = _frontmatter(body)

    assert frontmatter["name"] == name
    assert frontmatter["metadata"]["gobby"]["audience"] == "interactive"
    assert frontmatter["metadata"]["gobby"]["depth"] == 0
    assert command in body
    assert "I/D" in body
    assert re.search(r"\bI\)\s*Interactive\b", body) is not None
    assert re.search(r"\bD\)\s*Delegated\b", body) is not None
    assert 'value="interactive" | "delegated"' in body
    assert agent in body
    assert workflow_path in body


def _frontmatter(body: str) -> dict[str, Any]:
    match = re.match(r"^---\n(?P<yaml>.*?)\n---\n", body, flags=re.DOTALL)
    assert match is not None, "skill should use YAML frontmatter"
    data = yaml.safe_load(match.group("yaml"))
    assert isinstance(data, dict)
    return data
