"""Phase 2 contract tests for merge-agent lifecycle write-back."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry

pytestmark = pytest.mark.unit


def _agent(name: str) -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / f"src/gobby/install/shared/workflows/agents/{name}.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _allowed_mcp_tools(agent: dict) -> set[str]:
    tools: set[str] = set()
    for step in agent.get("steps", []):
        tools.update(step.get("allowed_mcp_tools", []) or [])
    return tools


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock()
    manager.db = MagicMock()
    manager.get_task.return_value = MagicMock(id="task-1", status="open", lifecycle="merging")
    return manager


@pytest.fixture
def lifecycle_registry(mock_task_manager: MagicMock):
    return create_lifecycle_registry(
        RegistryContext(task_manager=mock_task_manager, sync_manager=MagicMock())
    )


def test_merge_orchestrator_allowlist_includes_merge_tools() -> None:
    assert {
        "gobby-tasks:mark_task_merged",
        "gobby-tasks:mark_task_merge_failed",
    } <= _allowed_mcp_tools(_agent("merge-orchestrator"))


def test_merge_worker_allowlist_includes_merge_tools() -> None:
    assert {
        "gobby-tasks:mark_task_merged",
        "gobby-tasks:mark_task_merge_failed",
    } <= _allowed_mcp_tools(_agent("merge-worker"))


def test_pr_url_artifact_set_on_merge_via_tool(lifecycle_registry, mock_task_manager) -> None:
    tool = lifecycle_registry._tools["mark_task_merged"].func
    tool(task_id="#1", pr_url="https://example.test/pr/1")

    mock_task_manager.mark_task_merged.assert_called_once_with(
        "#1",
        pr_url="https://example.test/pr/1",
        merge_sha=None,
    )


def test_conflict_retry_within_max_merge_attempts(lifecycle_registry, mock_task_manager) -> None:
    tool = lifecycle_registry._tools["mark_task_merge_failed"].func
    tool(task_id="#1", reason="conflict")

    mock_task_manager.mark_task_merge_failed.assert_called_once_with("#1", reason="conflict")


def test_unattended_fallback_on_unresolved_conflict() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction
    from gobby.dispatch.rules import merge_rule

    task = type(
        "Task",
        (),
        {
            "id": "task-1",
            "ref": "#1",
            "task_type": "epic",
            "lifecycle": "merging",
            "status": "open",
            "labels": [],
            "unattended": True,
            "dispatch_failure_count": 3,
        },
    )()
    context = type("Context", (), {"artifacts": object(), "max_merge_attempts": 3})()

    assert isinstance(merge_rule(task, context), AdvanceLifecycleAction)
