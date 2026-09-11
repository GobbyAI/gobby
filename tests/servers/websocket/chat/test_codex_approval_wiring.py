"""Exercise approval routing through the actual app-server RPC dispatcher."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.client_rpc import handle_incoming_request
from gobby.agents.sandbox import SandboxConfig
from gobby.servers.websocket.chat.backends.codex import (
    CodexManagedChatSession,
    CodexWebChatBackend,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("chat_mode", ["bypass", "accept_edits"])
async def test_attached_client_routes_mcp_approval_through_lifecycle(
    tmp_path: Path, shared: bool, blocked: bool, chat_mode: str
) -> None:
    client = CodexAppServerClient()
    backend = CodexWebChatBackend(
        client=client if shared else None,
        client_factory=lambda: client,
    )
    session = CodexManagedChatSession(
        conversation_id="approval-test",
        _backend=backend,
        sandbox_config=SandboxConfig(enabled=False),
    )
    session.project_path = str(tmp_path)
    session.chat_mode = chat_mode
    lifecycle = AsyncMock(return_value={"decision": "block"} if blocked else None)
    session._on_pre_tool = lifecycle
    responses: list[dict[str, Any]] = []

    async def capture_response(response: dict[str, Any]) -> None:
        responses.append(response)

    with (
        patch("gobby.servers.websocket.chat.backends.codex.shutil.which", return_value="codex"),
        patch.object(client, "start", new=AsyncMock()),
        patch.object(client, "start_thread", new=AsyncMock(return_value=SimpleNamespace(id="t1"))),
        patch.object(client, "_send_stdin_response", new=capture_response),
    ):
        await backend.attach_session(session)
        await handle_incoming_request(
            client,
            {
                "id": 17,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": "t1",
                    "elicitationId": "e1",
                    "serverName": "gobby",
                    "message": 'Allow the gobby MCP server to run tool "get_tool_schema"?',
                    "_meta": {
                        "codex_approval_kind": "mcp_tool_call",
                        "tool_params": {"server_name": "gobby-tasks", "tool_name": "get_task"},
                    },
                },
            },
        )
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 17,
            "result": {"action": "cancel" if blocked else "accept", "content": None, "_meta": None},
        }
    ]
    assert session.sdk_session_id == "t1"
    lifecycle.assert_awaited_once_with(
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "get_task"},
        }
    )
