"""Unit tests for shared tool approval helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.servers.tool_approvals import (
    find_out_of_repo_write_path,
    is_builtin_auto_exempt,
    normalize_stored_approval_key,
)

pytestmark = pytest.mark.unit


def test_normalize_stored_approval_key_ignores_malformed_call_tool_key() -> None:
    assert normalize_stored_approval_key("call_tool:legacy") == ""


@pytest.mark.parametrize(
    ("raw_key", "expected"),
    [
        ("call_tool:legacy:gobby:do_thing", "mcp:gobby:do_thing"),
        ("", ""),
        ("call_tool:legacy", ""),
        ("mcp__gobby-like__do_thing", "mcp:gobby-like:do_thing"),
    ],
)
def test_normalize_stored_approval_key_cases(raw_key: str, expected: str) -> None:
    assert normalize_stored_approval_key(raw_key) == expected


def test_is_builtin_auto_exempt_allows_known_gobby_servers() -> None:
    assert is_builtin_auto_exempt("mcp__gobby__do_thing", {})


def test_is_builtin_auto_exempt_rejects_unknown_gobby_like_server() -> None:
    assert not is_builtin_auto_exempt("mcp__gobby-evil__do_thing", {})


def test_find_out_of_repo_write_path_allows_in_repo_plan_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = find_out_of_repo_write_path(
        "Write",
        {"file_path": ".gobby/plans/plan.md"},
        project_path=str(repo),
    )

    assert result is None


@pytest.mark.parametrize(
    "file_path",
    [
        "../../.gobby/plans/plan.md",
        ".claude/plans/plan.md",
    ],
)
def test_find_out_of_repo_write_path_blocks_external_plan_file(
    tmp_path: Path,
    file_path: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = str(tmp_path / file_path) if file_path.startswith(".claude") else file_path

    result = find_out_of_repo_write_path(
        "Write",
        {"file_path": candidate},
        project_path=str(repo),
    )

    assert result == candidate
