"""Unit tests for shared tool approval helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.servers.tool_approvals import (
    are_plan_mode_write_paths_allowed,
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


@pytest.mark.unit
def test_is_builtin_auto_exempt_allows_known_gobby_servers() -> None:
    assert is_builtin_auto_exempt("mcp__gobby__get_tool_schema", {})
    assert is_builtin_auto_exempt(
        "mcp__gobby__call_tool",
        {
            "server_name": "gobby-results",
            "tool_name": "get_tool_result",
        },
    )


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


def test_plan_mode_blocks_project_local_provider_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert not are_plan_mode_write_paths_allowed(
        "Write",
        {"file_path": ".claude/plans/design.md"},
        provider="claude",
        project_path=str(repo),
    )


@pytest.mark.parametrize(
    "file_path",
    [
        "../../.gobby/plans/plan.md",
        str(Path.home() / ".codex" / "plans" / "plan.md"),
    ],
)
def test_find_out_of_repo_write_path_blocks_external_plan_file(
    tmp_path: Path,
    file_path: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = file_path

    result = find_out_of_repo_write_path(
        "Write",
        {"file_path": candidate},
        project_path=str(repo),
    )

    assert result == candidate


@pytest.mark.parametrize(
    ("tool_name", "path_key"),
    [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")],
)
def test_plan_mode_structured_tools_allow_active_provider_home(
    tmp_path: Path,
    tool_name: str,
    path_key: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = Path.home() / ".codex" / "scratch" / "state.json"

    assert are_plan_mode_write_paths_allowed(
        tool_name,
        {path_key: str(target)},
        provider="codex",
        project_path=str(repo),
    )


def test_plan_mode_structured_write_allows_os_temp(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert are_plan_mode_write_paths_allowed(
        "Write",
        {"file_path": str(tmp_path / "scratch" / "state.json")},
        provider="claude",
        project_path=str(repo),
    )


def test_plan_mode_multi_file_write_fails_closed_for_mixed_targets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    approved = Path.home() / ".codex" / "scratch" / "state.json"
    cross_provider = Path.home() / ".claude" / "scratch" / "state.json"

    assert not are_plan_mode_write_paths_allowed(
        "Write",
        {"changes": [{"path": str(approved)}, {"path": str(cross_provider)}]},
        provider="codex",
        project_path=str(repo),
    )


def test_out_of_repo_checker_exempts_scratch_only_with_plan_provider(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = str(Path.home() / ".codex" / "scratch" / "state.json")

    assert (
        find_out_of_repo_write_path(
            "Write",
            {"file_path": target},
            project_path=str(repo),
            plan_scratch_provider="codex",
        )
        is None
    )
    assert (
        find_out_of_repo_write_path(
            "Write",
            {"file_path": target},
            project_path=str(repo),
        )
        == target
    )


def test_out_of_repo_checker_returns_unsafe_path_from_mixed_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    approved = str(Path.home() / ".codex" / "scratch" / "state.json")
    unsafe = str(Path.home() / ".claude" / "scratch" / "state.json")

    result = find_out_of_repo_write_path(
        "Write",
        {"file_paths": [approved, unsafe]},
        project_path=str(repo),
        plan_scratch_provider="codex",
    )

    assert result == unsafe
