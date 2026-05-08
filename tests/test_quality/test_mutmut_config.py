"""Smoke tests for focused mutation-testing configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_mutmut_dev_dependency_and_focused_config() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dev_dependencies = pyproject["dependency-groups"]["dev"]
    mutmut_config = pyproject["tool"]["mutmut"]

    assert any(dependency.startswith("mutmut>=") for dependency in dev_dependencies)
    assert mutmut_config["paths_to_mutate"] == ["src/gobby"]
    assert mutmut_config["pytest_add_cli_args_test_selection"] == ["tests/"]
    assert "pytest" in mutmut_config["runner"]


def test_documented_focused_mutmut_command_shape() -> None:
    command = 'uv run mutmut run "gobby.tasks.validation*"'

    assert command.startswith("uv run mutmut run ")
    assert '"gobby.tasks.validation*"' in command
