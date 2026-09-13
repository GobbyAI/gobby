"""Exercise the native Ask MCP transport with real signed run authentication."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from starlette.routing import Route

from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.servers.routes.ask_mcp import ASK_MCP_PATH, create_ask_mcp_app
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import issue_agent_api_token


async def exercise_http_mcp_boundary(
    server: HTTPServer,
    database: HubDatabase,
    tmp_path: Path,
    *,
    project_id: str,
    session_id: str,
    agent_run_id: str,
    ask_run_id: str,
) -> None:
    token_file = tmp_path / "mcp-operator-token"
    token_file.write_text("test-mcp-operator")
    AuthStore(database).set_local_api_token_hash(hash_token("test-mcp-operator"))
    server.auth_service = AuthService(lambda: database, token_file=token_file)
    token = issue_agent_api_token(
        "test-mcp-operator",
        agent_run_id=agent_run_id,
        session_id=session_id,
        project_id=project_id,
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": agent_run_id,
        "X-Gobby-Session-Id": session_id,
        "X-Gobby-Caller-Project-Id": project_id,
        "X-Gobby-Project-Id": project_id,
        "Accept": "application/json, text/event-stream",
    }
    transport_app = create_ask_mcp_app(server, host="127.0.0.1")
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)
    app.router.routes.append(Route(ASK_MCP_PATH, endpoint=transport_app))
    async with (
        transport_app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client,
    ):

        async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
            response = await client.post(
                ASK_MCP_PATH,
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            assert response.status_code == 200, response.text
            result: dict[str, Any] = response.json()["result"]
            return result

        initialized = await rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ask-test", "version": "1"},
            },
        )
        assert initialized["serverInfo"]["name"] == "gobby"
        listed = await rpc("tools/list", {})
        assert {tool["name"] for tool in listed["tools"]} == {
            "call_tool",
            "get_tool_schema",
            "list_tools",
        }

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await rpc("tools/call", {"name": name, "arguments": arguments})
            assert not result.get("isError"), result
            content: dict[str, Any] = result["structuredContent"]
            return content

        visible = await call("list_tools", {"server_name": "gobby-ask"})
        assert {tool["name"] for tool in visible["tools"]} == {"read_evidence"}
        hidden = await call("list_tools", {"server_name": "gobby-tasks"})
        assert hidden["tools"] == []
        schema = await call(
            "get_tool_schema", {"server_name": "gobby-ask", "tool_name": "read_evidence"}
        )
        assert schema["success"] is True
        accepted = await call(
            "call_tool",
            {
                "server_name": "gobby-ask",
                "tool_name": "read_evidence",
                "arguments": {"run_id": ask_run_id},
            },
        )
        assert accepted["success"] is True
        assert accepted["result"] == {"evidence": ["x" * 20_000]}
        for server_name, tool_name, arguments in (
            ("gobby-tasks", "close_task", {"task_id": "#1"}),
            ("gobby-ask", "submit_review", {"run_id": ask_run_id}),
            ("gobby-ask", "read_evidence", {"run_id": "00000000-0000-4000-8000-000000000001"}),
        ):
            denied = await call(
                "call_tool",
                {"server_name": server_name, "tool_name": tool_name, "arguments": arguments},
            )
            assert denied["success"] is False
            assert denied["error_code"] == "TOOL_BLOCKED"

        for header, value in (
            ("Authorization", "Bearer invalid"),
            ("X-Gobby-Session-Id", "00000000-0000-4000-8000-000000000001"),
            ("X-Gobby-Agent-Run-Id", "00000000-0000-4000-8000-000000000001"),
            ("X-Gobby-Caller-Project-Id", "00000000-0000-4000-8000-000000000001"),
        ):
            response = await client.post(
                ASK_MCP_PATH,
                headers={**headers, header: value},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 401, (header, response.text)

        # Target project is not caller identity: the shared MCP auth contract
        # admits it, then the tool boundary must refuse the foreign target.
        response = await client.post(
            ASK_MCP_PATH,
            headers={**headers, "X-Gobby-Project-Id": "00000000-0000-4000-8000-000000000001"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "call_tool",
                    "arguments": {
                        "server_name": "gobby-ask",
                        "tool_name": "read_evidence",
                        "arguments": {"run_id": ask_run_id},
                    },
                },
            },
        )
        assert response.status_code == 200
        denied_result = response.json()["result"]
        assert denied_result.get("isError") or not denied_result["structuredContent"]["success"]
