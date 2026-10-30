"""Git apply modes must retain the distinction between index and file writes."""

from pathlib import Path
from typing import Any

import pytest

from gobby.hooks._normalization_tools import _set_canonical_tool_metadata

pytestmark = pytest.mark.unit


def invocation(command: str, cwd: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "cwd": str(cwd)},
        "project_path": str(cwd),
    }
    _set_canonical_tool_metadata(data)
    return data


@pytest.mark.parametrize(
    "arguments",
    [
        "--check change.patch",
        "--stat change.patch",
        "--numstat change.patch",
        "--summary change.patch",
        "--cached --check change.patch",
        "--cached --reject --check change.patch",
        "-h",
        "--help",
    ],
)
def test_git_apply_inspection_does_not_mutate(tmp_path: Path, arguments: str) -> None:
    data = invocation(f"git apply {arguments}", tmp_path)
    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")


@pytest.mark.parametrize(
    "arguments",
    [
        "change.patch",
        "--stat --apply change.patch",
        "--apply --stat change.patch",
        "--include --check change.patch",
        "--directory --stat change.patch",
        "-- --check",
        "--cached --unknown change.patch",
        "--cached --whitespace",
        "--check --build-fake-ancestor fake-index change.patch",
        "--cached --build-fake-ancestor=fake-index change.patch",
        "--cached --reject change.patch",
        "--cached --reject --check --apply change.patch",
        '--cached "$options" change.patch',
        "--cached *",
    ],
)
def test_git_apply_unproven_worktree_writes_stay_unknown(tmp_path: Path, arguments: str) -> None:
    data = invocation(f"git apply {arguments}", tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert data["canonical_repo_mutation_scope_unknown"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        "--cached change.patch",
        "--cached --3way change.patch",
        "--cached --stat --apply change.patch",
        "--cached --reverse -p1 -C2 --whitespace=fix change.patch",
        "--cached --include owned.py --exclude ignored.py change.patch",
    ],
)
def test_git_apply_cached_only_mutates_index(tmp_path: Path, arguments: str) -> None:
    data = invocation(f"git apply {arguments}", tmp_path)
    assert data["canonical_tool_kind"] == "execute"
    assert data["canonical_repo_mutation"] is True
    assert data.get("canonical_edit_file_paths", []) == []
    assert not data.get("canonical_repo_mutation_scope_unknown")


def test_git_apply_dynamic_index_selector_stays_unknown(tmp_path: Path) -> None:
    data = invocation('GIT_INDEX_FILE="$index" git apply --cached change.patch', tmp_path)
    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation_scope_unknown"] is True
