from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import gobby.mcp_proxy.tools.tasks._delivery as delivery_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import create_task

pytestmark = pytest.mark.unit


def _registry(temp_db: Any) -> InternalToolRegistry:
    return create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
    )


class FakeGitHub:
    def __init__(self, list_result: Any | None = None) -> None:
        self.list_result = [] if list_result is None else list_result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        assert server_name == "github"
        self.calls.append((tool_name, arguments))
        if tool_name == "list_pull_requests":
            return self.list_result
        if tool_name == "create_pull_request":
            return {
                "html_url": "https://github.com/test/test-project/pull/7",
                "number": 7,
                "state": "open",
            }
        raise AssertionError(f"unexpected GitHub tool: {tool_name}")


def _registry_with_github(temp_db: Any, github: FakeGitHub) -> InternalToolRegistry:
    return create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        mcp_manager=cast(Any, github),
    )


def test_delivery_state_tools_are_registered(temp_db: Any) -> None:
    registry = _registry(temp_db)

    assert registry.get_tool("record_pr_state") is not None
    assert registry.get_tool("get_delivery_state") is not None
    assert registry.get_tool("open_delivery_pr") is not None


def test_record_pr_state_persists_delivery_unit(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    registry = _registry(temp_db)
    record = registry.get_tool("record_pr_state")
    get_state = registry.get_tool("get_delivery_state")
    assert record is not None
    assert get_state is not None

    result = record(
        task_id=task.id,
        worktree_id="wt-1",
        repo="owner/repo",
        source_branch="feature/task",
        target_branch="main",
        pr_required=True,
        protection={"requires_pr": True},
        pr_state="awaiting_ci",
        campaign_state="pr_open",
        merge_strategy="squash",
    )

    assert result["delivery"]["campaign"]["state"] == "pr_open"
    state = get_state(task_id=task.id)["delivery"]
    assert state["campaign"]["merge_strategy"] == "squash"
    assert state["units"][0]["unit_key"] == "worktree:wt-1"
    assert state["units"][0]["protection"] == {"requires_pr": True}
    assert state["units"][0]["pr_state"] == "awaiting_ci"


def test_record_pr_state_defaults_merge_strategy_for_direct_merge(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    registry = _registry(temp_db)
    record = registry.get_tool("record_pr_state")
    assert record is not None

    result = record(
        task_id=task.id,
        pr_required=False,
        pr_state="direct_merge",
        campaign_state="direct_merge",
    )

    assert result["delivery"]["campaign"]["state"] == "direct_merge"
    assert result["delivery"]["campaign"]["merge_strategy"] == "squash"
    assert result["delivery"]["units"][0]["pr_required"] is False
    assert result["delivery"]["units"][0]["pr_state"] == "direct_merge"


@pytest.mark.asyncio
async def test_open_delivery_pr_uses_github_mcp_for_same_repo(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    github = FakeGitHub()
    registry = _registry_with_github(temp_db, github)
    tool = registry.get_tool("open_delivery_pr")
    assert tool is not None

    result = await tool(
        task_id=task.id,
        source_branch="feature/task",
        target_branch="main",
        push=False,
    )

    assert result["ok"] is True
    assert result["created_via"] == "github_mcp"
    assert github.calls[0] == (
        "list_pull_requests",
        {
            "owner": "test",
            "repo": "test-project",
            "state": "open",
            "head": "feature/task",
            "base": "main",
            "per_page": 10,
        },
    )
    assert github.calls[1][0] == "create_pull_request"
    assert github.calls[1][1]["head"] == "feature/task"
    row = temp_db.fetchone(
        "SELECT repo, source_branch, target_branch, pr_url, github_pr_number "
        "FROM task_delivery_units WHERE task_id = %s",
        (task.id,),
    )
    assert row["repo"] == "test/test-project"
    assert row["source_branch"] == "feature/task"
    assert row["target_branch"] == "main"
    assert row["pr_url"] == "https://github.com/test/test-project/pull/7"
    assert row["github_pr_number"] == 7


@pytest.mark.asyncio
async def test_open_delivery_pr_reuses_existing_github_pr(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    github = FakeGitHub(
        list_result=[
            {
                "html_url": "https://github.com/test/test-project/pull/3",
                "number": 3,
                "state": "open",
            }
        ]
    )
    registry = _registry_with_github(temp_db, github)
    tool = registry.get_tool("open_delivery_pr")
    assert tool is not None

    result = await tool(
        task_id=task.id,
        source_branch="feature/reused",
        target_branch="main",
        push=False,
    )

    assert result["ok"] is True
    assert result["reused"] is True
    assert result["pr_url"] == "https://github.com/test/test-project/pull/3"
    assert [name for name, _args in github.calls] == ["list_pull_requests"]


@pytest.mark.asyncio
async def test_open_delivery_pr_reuses_local_delivery_unit(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    github = FakeGitHub()
    registry = _registry_with_github(temp_db, github)
    record = registry.get_tool("record_pr_state")
    tool = registry.get_tool("open_delivery_pr")
    assert record is not None
    assert tool is not None
    record(
        task_id=task.id,
        source_branch="feature/local",
        target_branch="main",
        pr_url="https://github.com/test/test-project/pull/11",
        github_pr_number=11,
        pr_state="open",
    )

    result = await tool(
        task_id=task.id,
        source_branch="feature/local",
        target_branch="main",
        push=False,
    )

    assert result["ok"] is True
    assert result["reused"] is True
    assert result["created_via"] == "delivery_state"
    assert result["pushed"] is False
    assert result["pr_url"] == "https://github.com/test/test-project/pull/11"
    assert github.calls == []


@pytest.mark.asyncio
async def test_open_delivery_pr_uses_rest_head_repo_for_same_org_cross_repo(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    github = FakeGitHub()
    captured: dict[str, Any] = {}

    async def fake_rest(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "html_url": "https://github.com/org/target/pull/9",
            "number": 9,
            "state": "open",
        }

    monkeypatch.setattr(delivery_tools, "_create_pull_request_rest", fake_rest)
    registry = _registry_with_github(temp_db, github)
    tool = registry.get_tool("open_delivery_pr")
    assert tool is not None

    result = await tool(
        task_id=task.id,
        source_repo="org/source",
        target_repo="org/target",
        source_branch="feature/cross",
        target_branch="main",
        push=False,
    )

    assert result["ok"] is True
    assert result["created_via"] == "github_rest"
    assert captured["head"] == "org:feature/cross"
    assert captured["head_repo"] == "source"
    assert [name for name, _args in github.calls] == ["list_pull_requests"]
    row = temp_db.fetchone(
        "SELECT repo, source_branch, target_branch, pr_url, github_pr_number "
        "FROM task_delivery_units WHERE task_id = %s",
        (task.id,),
    )
    assert row["repo"] == "org/target"
    assert row["source_branch"] == "feature/cross"
    assert row["target_branch"] == "main"
    assert row["pr_url"] == "https://github.com/org/target/pull/9"
    assert row["github_pr_number"] == 9


def test_push_branch_rejects_invalid_branch_ref(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source_branch is not a valid git branch ref"):
        delivery_tools._push_branch(
            repo_path=str(tmp_path),
            source_branch="bad branch",
            remote_branch="feature/good",
            force_with_lease=False,
        )


def test_github_token_logs_environment_source(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")

    with caplog.at_level(logging.DEBUG):
        token = delivery_tools._github_token(object())

    assert token == "secret"
    assert "Using GitHub token from environment variable GH_TOKEN" in caplog.text


def test_github_token_logs_missing_and_lookup_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name in delivery_tools._GITHUB_TOKEN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    with caplog.at_level(logging.DEBUG):
        token = delivery_tools._github_token(object())

    assert token is None
    assert "No GitHub token found in environment; checking stored secrets" in caplog.text
    assert "GitHub token lookup from secret store failed" in caplog.text
