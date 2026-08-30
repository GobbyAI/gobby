"""OpenAPI MCP negotiation smoke through gobby's real stdio transport.

Skip-by-default (`uvx` missing or `GOBBY_OPENAPI_SMOKE` unset) so CI never
spawns the pinned AWS OpenAPI server.

Observed negotiation path against `awslabs.openapi-mcp-server@1.1.5`
(FastMCP 3 / mcp 1.x stdio): gobby's mcp 2.x `Client` probes
`server/discover`, then falls back to the legacy `initialize` handshake
before `list_tools` and `call_tool` succeed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.client_manager.tool_inventory import list_tools_from_session
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.transports.stdio import StdioTransportConnection

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_PETSTORE_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "openapi" / "petstore.json"
_CONNECT_WAIT_S = 180.0
_DISCONNECT_WAIT_S = 10.0
_HTTP_JOIN_S = 5.0


def _require_openapi_smoke() -> None:
    if shutil.which("uvx") is None:
        pytest.skip("uvx is not installed")
    if os.environ.get("GOBBY_OPENAPI_SMOKE") != "1":
        pytest.skip("set GOBBY_OPENAPI_SMOKE=1 to run OpenAPI stdio negotiation smoke")


@pytest.fixture(autouse=True)
def _openapi_smoke_gate() -> None:
    _require_openapi_smoke()


class _PetstoreServer(ThreadingHTTPServer):
    spec_body: bytes
    pets_body: bytes
    pet_body: bytes


class _PetstoreHandler(BaseHTTPRequestHandler):
    server: _PetstoreServer

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/openapi.json":
            body = self.server.spec_body
        elif path == "/pets":
            body = self.server.pets_body
        elif path.startswith("/pets/"):
            body = self.server.pet_body
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass(frozen=True, slots=True)
class _PetstoreHttp:
    httpd: _PetstoreServer
    thread: threading.Thread
    base: str


def _stop_http(httpd: ThreadingHTTPServer, thread: threading.Thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(_HTTP_JOIN_S)


@pytest.fixture
def petstore_http() -> Generator[_PetstoreHttp]:
    spec = json.loads(_PETSTORE_FIXTURE.read_text(encoding="utf-8"))
    httpd = _PetstoreServer(("127.0.0.1", 0), _PetstoreHandler)
    port = httpd.server_address[1]
    assert isinstance(port, int)
    spec["servers"] = [{"url": f"http://127.0.0.1:{port}"}]
    httpd.spec_body = json.dumps(spec).encode()
    httpd.pets_body = json.dumps({"pets": [{"id": 1, "name": "rex"}]}).encode()
    httpd.pet_body = json.dumps({"id": 1, "name": "rex"}).encode()

    thread = threading.Thread(target=httpd.serve_forever, name="petstore-http", daemon=True)
    thread.start()
    try:
        yield _PetstoreHttp(httpd=httpd, thread=thread, base=f"http://127.0.0.1:{port}")
    finally:
        _stop_http(httpd, thread)
        assert not thread.is_alive(), "petstore HTTP thread still alive after join"


def _petstore_config(base: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="petstore",
        project_id=str(uuid.uuid4()),
        transport="stdio",
        command="uvx",
        args=["awslabs.openapi-mcp-server@1.1.5", "--log-level", "ERROR"],
        env={
            "API_NAME": "petstore",
            "API_BASE_URL": base,
            "API_SPEC_URL": f"{base}/openapi.json",
            "ALLOW_INSECURE_HTTP": "true",
            "ALLOW_PRIVATE_NETWORKS": "true",
        },
        connect_timeout=120.0,
    )


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if result.structured_content is not None:
        parts.append(json.dumps(result.structured_content))
    return "\n".join(parts)


async def _disconnect_stdio(connection: StdioTransportConnection) -> None:
    await asyncio.wait_for(connection.disconnect(), _DISCONNECT_WAIT_S)
    assert not connection.is_connected


async def _run_connected_session(
    http: _PetstoreHttp,
    body: Callable[[StdioTransportConnection], Awaitable[None]],
) -> None:
    connection = StdioTransportConnection(_petstore_config(http.base))
    try:
        await asyncio.wait_for(connection.connect(), _CONNECT_WAIT_S)
        assert connection.is_connected
        await body(connection)
    finally:
        await _disconnect_stdio(connection)
        _stop_http(http.httpd, http.thread)
        assert not http.thread.is_alive(), "petstore HTTP thread still alive after cleanup"


@pytest.mark.asyncio
async def test_openapi_server_negotiates_and_serves_tools(petstore_http: _PetstoreHttp) -> None:
    """Initialize → list_tools → call_tool through StdioTransportConnection.

    Negotiation observed: `server/discover` probe, then legacy `initialize`
    fallback (OpenAPI server is mcp 1.x; gobby Client is mcp 2.x).
    """

    async def _exercise(connection: StdioTransportConnection) -> None:
        session = connection.session
        assert session is not None
        tools = await list_tools_from_session(session)
        list_pets = next(tool for tool in tools if tool["name"] == "listPets")
        assert isinstance(list_pets["inputSchema"], dict)
        result = await session.call_tool("listPets", {})
        assert result.is_error is False
        assert "rex" in _result_text(result)

    await _run_connected_session(petstore_http, _exercise)


@pytest.mark.asyncio
async def test_openapi_server_disconnect_kills_child(petstore_http: _PetstoreHttp) -> None:
    """Forced failure after connect still disconnects uvx and joins HTTP."""

    async def _fail_after_connect(connection: StdioTransportConnection) -> None:
        assert connection.is_connected
        raise RuntimeError("forced failure after connect")

    with pytest.raises(RuntimeError, match="forced failure after connect"):
        await _run_connected_session(petstore_http, _fail_after_connect)
    assert not petstore_http.thread.is_alive()
