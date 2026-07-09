"""AuthMiddleware exemption behavior for machine-local tooling routes.

UI auth must never sever the data-plane routes the installed gcode/gwiki
binaries and the MCP proxy call: a 401 on /api/llm/generate previously made
`gcode codewiki --ai auto` resolve "no daemon" and destructively rewrite an
AI-generated vault as structural docs (#17777, #17776).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.middleware.auth import AuthMiddleware


@pytest.fixture
def client() -> TestClient:
    """App with AuthMiddleware and a catch-all route that returns 200."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=None)

    @app.get("/{path:path}")
    @app.post("/{path:path}")
    async def catch_all(path: str) -> dict[str, str]:
        return {"path": path}

    return TestClient(app)


TOOLING_PATHS = [
    "/api/llm/generate",
    "/api/llm/vision/extract",
    "/api/embeddings",
    "/api/voice/transcribe",
    "/api/workflows/variables/set",
    "/api/workflows/variables/get",
    "/api/mcp/tools/call",
    "/api/sessions/register",
]


@pytest.mark.parametrize("path", TOOLING_PATHS)
def test_tooling_routes_bypass_ui_auth(client: TestClient, path: str) -> None:
    """Machine-local tooling routes pass even with auth enabled and no cookie."""
    with (
        patch("gobby.servers.routes.auth.is_auth_enabled", return_value=True),
        patch("gobby.servers.routes.auth.validate_session_cookie", return_value=False),
    ):
        response = client.post(path)

    assert response.status_code == 200, path


@pytest.mark.parametrize("path", ["/api/tasks", "/api/config", "/api/agents"])
def test_ui_api_routes_require_auth_when_enabled(client: TestClient, path: str) -> None:
    """Non-exempt API routes 401 when auth is enabled and no session cookie."""
    with (
        patch("gobby.servers.routes.auth.is_auth_enabled", return_value=True),
        patch("gobby.servers.routes.auth.validate_session_cookie", return_value=False),
    ):
        response = client.get(path)

    assert response.status_code == 401, path
    assert response.json() == {"error": "Authentication required"}


def test_all_routes_pass_when_auth_not_configured(client: TestClient) -> None:
    """Local-first default: without configured credentials nothing is gated."""
    with patch("gobby.servers.routes.auth.is_auth_enabled", return_value=False):
        response = client.get("/api/tasks")

    assert response.status_code == 200
