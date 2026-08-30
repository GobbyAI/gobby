"""Direct manager consumers resolve by project scope and dispatch by id (4.2.6)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.github_triage.service import GitHubIssueTriageService
from gobby.integrations.github import GitHubIntegration
from gobby.integrations.github_helper import GitHubMCPHelper
from gobby.integrations.linear import LinearIntegration
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._delivery import _find_existing_pr
from gobby.servers.websocket.handlers.core import HandlerMixin
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.sync.github import GitHubSyncService
from gobby.sync.github_issue_sync import GitHubIssueSyncService
from gobby.sync.linear import LinearSyncService
from gobby.sync.task_github_import import GitHubIssueImporter
from tests.mcp_proxy.services.test_scope_resolution_matrix import (
    GLOBAL_SERVER_ID,
    PROJECT_ID,
    PROJECT_SERVER_ID,
    RecordingManager,
    as_mcp,
    scoped_github_configs,
)

pytestmark = pytest.mark.unit

LINEAR_PROJECT_ID = PROJECT_SERVER_ID
LINEAR_GLOBAL_ID = GLOBAL_SERVER_ID


def _linear_configs() -> list[MCPServerConfig]:
    return [
        MCPServerConfig(
            name="linear",
            project_id=PROJECT_ID,
            url="https://linear-project.example.test",
            id=LINEAR_PROJECT_ID,
            enabled=True,
        ),
        MCPServerConfig(
            name="linear",
            project_id=GLOBAL_PROJECT_ID,
            url="https://linear-global.example.test",
            id=LINEAR_GLOBAL_ID,
            enabled=True,
        ),
    ]


def _assert_project_id(manager: RecordingManager, *methods: str) -> None:
    seen: list[str] = []
    for method in methods:
        seen.extend(manager.method_ids(method))
    assert PROJECT_SERVER_ID in seen or LINEAR_PROJECT_ID in seen
    assert GLOBAL_SERVER_ID not in seen
    assert LINEAR_GLOBAL_ID not in seen or LINEAR_PROJECT_ID in seen


@pytest.mark.asyncio
async def test_consumers_resolve_project_instance_by_id() -> None:
    github_manager = RecordingManager(scoped_github_configs(), project_id=PROJECT_ID)
    linear_manager = RecordingManager(_linear_configs(), project_id=PROJECT_ID)

    github = GitHubIntegration(as_mcp(github_manager), project_id=PROJECT_ID)
    assert github.is_available() is True
    _assert_project_id(github_manager, "has_server")

    linear = LinearIntegration(as_mcp(linear_manager), project_id=PROJECT_ID)
    assert linear.is_available() is True
    _assert_project_id(linear_manager, "has_server")

    helper = GitHubMCPHelper(
        as_mcp(github_manager),
        repo_path="/tmp/repo",
        github_repo="owner/repo",
        project_id=PROJECT_ID,
    )
    await helper._call_github_mcp("list_issues", {"owner": "owner", "repo": "repo"})
    _assert_project_id(github_manager, "get_client_session", "call_tool")

    sync = GitHubSyncService(
        mcp_manager=as_mcp(github_manager),
        task_manager=MagicMock(),
        project_id=PROJECT_ID,
        github_repo="owner/repo",
    )
    github_manager.calls.clear()
    await sync._call_github_mcp("list_issues", {"owner": "owner", "repo": "repo"})
    _assert_project_id(github_manager, "call_tool")

    triage = GitHubIssueTriageService(db=MagicMock(), mcp_manager=as_mcp(github_manager))
    github_manager.calls.clear()
    await triage._github_call("get_issue", {"owner": "o", "repo": "r", "issue_number": 1})
    _assert_project_id(github_manager, "call_tool", "get_client_session")

    issue_sync = GitHubIssueSyncService(db=MagicMock(), mcp_manager=as_mcp(github_manager))
    github_manager.calls.clear()
    with patch(
        "gobby.sync.github_issue_sync.parse_github_mcp_result",
        return_value={"ok": True},
    ):
        await issue_sync._call("get_issue", {"owner": "o", "repo": "r", "issue_number": 1})
    _assert_project_id(github_manager, "call_tool")

    class _Handler(HandlerMixin):
        def __init__(self, manager: RecordingManager) -> None:
            self.mcp_manager = as_mcp(manager)
            self.internal_manager = None
            self.project_id = PROJECT_ID

        async def broadcast_autonomous_event(
            self, event: str, session_id: str, **kwargs: Any
        ) -> None:
            return None

    handler = _Handler(github_manager)
    websocket = MagicMock()
    websocket.send = AsyncMock()
    github_manager.calls.clear()
    await handler._handle_tool_call(
        websocket,
        {
            "request_id": "req-1",
            "mcp": "github",
            "tool": "list_issues",
            "args": {},
        },
    )
    _assert_project_id(github_manager, "call_tool")
    github_manager.calls.clear()
    unknown = await handler._call_external_mcp("missing-server", "ping", {})
    assert unknown["success"] is False
    assert unknown["error_code"] == "SERVER_NOT_FOUND"
    assert github_manager.method_ids("call_tool") == []

    ctx = SimpleNamespace(mcp_manager=github_manager, project_id=PROJECT_ID)
    github_manager.calls.clear()
    await _find_existing_pr(cast(RegistryContext, ctx), "owner", "repo", "head", "main")
    _assert_project_id(github_manager, "call_tool")

    importer = GitHubIssueImporter(db=MagicMock())
    app_ctx = SimpleNamespace(mcp_manager=github_manager)
    github_manager.calls.clear()
    with patch("gobby.app_context.get_app_context", return_value=app_ctx):
        await importer._fetch_github_issues_mcp("owner", "repo", 10, project_id=PROJECT_ID)
    _assert_project_id(github_manager, "get_client_session", "call_tool", "has_server")

    linear_svc = LinearSyncService(
        mcp_manager=as_mcp(linear_manager),
        task_manager=MagicMock(),
        project_id=PROJECT_ID,
    )
    linear_manager.calls.clear()
    linear_svc.linear = LinearIntegration(as_mcp(linear_manager), project_id=PROJECT_ID)
    with (
        patch.object(linear_svc, "_linear_mcp_has_tool", return_value=True),
        patch.object(linear_svc, "_get_graphql_client", new_callable=AsyncMock, return_value=None),
        patch("gobby.sync.linear_project_ops._extract_records", return_value=[]),
    ):
        await linear_svc.list_teams()
    _assert_project_id(linear_manager, "call_tool", "has_server")
