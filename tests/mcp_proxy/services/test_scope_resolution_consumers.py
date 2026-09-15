"""Direct manager consumers resolve by project scope and dispatch by id (4.2.6)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket.handlers.core import HandlerMixin
from tests.mcp_proxy.services.test_scope_resolution_matrix import (
    GLOBAL_SERVER_ID,
    PROJECT_ID,
    PROJECT_SERVER_ID,
    RecordingManager,
    as_mcp,
    scoped_github_configs,
)

pytestmark = pytest.mark.unit


def _assert_only_id(manager: RecordingManager, expected_id: str, *methods: str) -> None:
    seen: list[str] = []
    for method in methods:
        seen.extend(manager.method_ids(method))
    assert set(seen) == {expected_id}


@pytest.mark.asyncio
async def test_consumers_resolve_project_instance_by_id() -> None:
    github_manager = RecordingManager(scoped_github_configs(), project_id=PROJECT_ID)

    class _Handler(HandlerMixin):
        def __init__(self, manager: RecordingManager) -> None:
            self.mcp_manager = as_mcp(manager)
            self.internal_manager = None
            self.project_id: str | None = PROJECT_ID

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
    _assert_only_id(github_manager, PROJECT_SERVER_ID, "call_tool")
    github_manager.calls.clear()
    unknown = await handler._call_external_mcp("missing-server", "ping", {})
    assert unknown["success"] is False
    assert unknown["error_code"] == "SERVER_NOT_FOUND"
    assert github_manager.method_ids("call_tool") == []

    sessionless = _Handler(github_manager)
    sessionless.project_id = None
    github_manager.calls.clear()
    await sessionless._call_external_mcp("github", "list_issues", {})
    _assert_only_id(github_manager, GLOBAL_SERVER_ID, "call_tool")


@pytest.mark.asyncio
async def test_resolved_server_id_rejects_missing_scope() -> None:
    from gobby.mcp_proxy.services.server_resolution import (
        ProjectScopeUnresolvedError,
        resolved_server_id,
    )

    manager = RecordingManager(scoped_github_configs(), project_id=PROJECT_ID)
    for scope in ("", "   "):
        with pytest.raises(ProjectScopeUnresolvedError):
            resolved_server_id(manager, "github", project_id=scope)
    assert manager.method_ids("call_tool") == []
