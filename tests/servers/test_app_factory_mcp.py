"""Tests for the daemon's streamable MCP HTTP route."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from gobby.servers.app_factory import _register_mcp_http_route


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
