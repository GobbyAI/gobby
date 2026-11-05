"""Tests for the daemon's streamable MCP HTTP route."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import HTTPConnection
from starlette.routing import Route
from starlette.testclient import TestClient

from gobby.servers.app_factory import _PrincipalBoundMcpApp, _register_mcp_http_route
from gobby.utils.session_context import get_request_principal


async def _mcp_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"path": request.url.path})


def test_mcp_http_route_uses_single_canonical_prefix() -> None:
    mcp_app = Starlette(routes=[Route("/mcp", endpoint=_mcp_endpoint, methods=["POST"])])
    app = FastAPI()

    _register_mcp_http_route(app, mcp_app)

    client = TestClient(app, follow_redirects=False)
    response = client.post("/mcp")

    assert response.status_code == 200
    assert response.json() == {"path": "/mcp"}
    assert client.post("/mcp/mcp").status_code == 404


class _AuthService:
    """Names the caller after a request header, standing in for credential checks."""

    def request_principal(self, request: HTTPConnection) -> str:
        return request.headers["x-test-caller"]


class _Server:
    auth_service = _AuthService()

    async def run_db(self, func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)


_JSON_RPC_HEADERS = {"Accept": "application/json, text/event-stream"}


def _rpc(client: TestClient, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    response = client.post("/mcp", json=payload, headers={**_JSON_RPC_HEADERS, **headers})
    assert response.status_code in {200, 202}, response.text
    return response


def test_mcp_route_seeds_each_requests_principal_for_tools() -> None:
    mcp = MCPServer("principal-probe")

    async def whoami() -> str:
        return str(await get_request_principal())

    mcp.add_tool(whoami)
    mcp_app = mcp.streamable_http_app(host="127.0.0.1")
    server: Any = _Server()
    app = FastAPI(lifespan=mcp_app.router.lifespan_context)
    _register_mcp_http_route(app, _PrincipalBoundMcpApp(server, mcp_app))

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        initialized = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"x-test-caller": "first-caller"},
        )
        session = {"mcp-session-id": initialized.headers["mcp-session-id"]}
        _rpc(
            client,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {**session, "x-test-caller": "first-caller"},
        )
        called = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
            {**session, "x-test-caller": "second-caller"},
        )

    assert '"text":"second-caller"' in called.text.replace(" ", "")
