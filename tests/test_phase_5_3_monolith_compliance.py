"""Phase 5.3 must be net deletion from legacy monolith files."""

from __future__ import annotations

import subprocess

import pytest

from tests.phase5_contract_helpers import ROOT, repo_path

pytestmark = pytest.mark.unit


def _line_count(path: str) -> int:
    return len(repo_path(path).read_text().splitlines())


def test_transitions_py_under_1000_lines_and_smaller_than_baseline() -> None:
    count = _line_count("src/gobby/storage/tasks/_transitions.py")

    assert count < 1000
    assert count < 784


def test_crud_py_under_1000_lines_and_smaller_than_baseline() -> None:
    count = _line_count("src/gobby/cli/tasks/crud.py")

    assert count < 1000
    assert count < 904


def test_routes_tasks_py_under_1000_lines_and_smaller_than_baseline() -> None:
    count = _line_count("src/gobby/servers/routes/tasks.py")

    assert count < 1000
    assert count < 699


def test_no_new_method_bodies_added_to_legacy_files() -> None:
    result = subprocess.run(
        [
            "git",
            "diff",
            "HEAD^",
            "--",
            "src/gobby/storage/tasks/_transitions.py",
            "src/gobby/cli/tasks/crud.py",
            "src/gobby/servers/routes/tasks.py",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    added_defs = [line for line in result.stdout.splitlines() if line.startswith("+def ")]

    assert added_defs == []
