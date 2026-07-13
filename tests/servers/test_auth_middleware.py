"""AuthMiddleware exemption behavior for machine-local tooling routes.

UI auth must never sever the data-plane routes the installed gcode/gwiki
binaries and the MCP proxy call: a 401 on /api/llm/generate previously made
`gcode codewiki --ai auto` resolve "no daemon" and destructively rewrite an
AI-generated vault as structural docs (#17777, #17776).
"""

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.middleware.auth import AuthMiddleware

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


@pytest.fixture
def auth_client() -> tuple[TestClient, MagicMock]:
    """App with AuthMiddleware and a catch-all route that returns 200."""
    app = FastAPI()
    auth_service = MagicMock()
    auth_service.enabled = True
    auth_service.is_request_authenticated.return_value = False
    app.add_middleware(
        AuthMiddleware,
        server=cast("HTTPServer", SimpleNamespace(auth_service=auth_service)),
    )

    @app.get("/{path:path}")
    @app.post("/{path:path}")
    async def catch_all(path: str) -> dict[str, str]:
        return {"path": path}

    return TestClient(app), auth_service


PROTECTED_PATHS = [
    "/api/llm/generate",
    "/api/llm/vision/extract",
    "/api/embeddings",
    "/api/voice/transcribe",
    "/api/workflows/variables/set",
    "/api/workflows/variables/get",
    "/api/mcp/tools/call",
    "/api/sessions/register",
    "/api/tasks",
    "/api/config",
    "/api/agents",
]

PUBLIC_PATHS = [
    "/",
    "/api/health",
    "/api/admin/health",
    "/api/admin/startup-progress",
    "/api/auth/status",
    "/api/comms/webhooks/test",
    "/api/github/webhooks/test",
    "/assets/app.js",
    "/favicon.ico",
    "/logo.png",
]


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_routes_bypass_required_auth(
    auth_client: tuple[TestClient, MagicMock], path: str
) -> None:
    client, auth_service = auth_client

    response = client.get(path)

    assert response.status_code == 200, path
    auth_service.is_request_authenticated.assert_not_called()


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_routes_require_auth_when_enabled(
    auth_client: tuple[TestClient, MagicMock], path: str
) -> None:
    """Data-plane and UI API routes require credentials in required mode."""
    client, _auth_service = auth_client

    response = client.get(path)

    assert response.status_code == 401, path
    assert response.json() == {
        "error": (
            "Authentication required. CLI clients need ~/.gobby/local_cli_token "
            "(run 'gobby install' or 'gobby auth token --rotate'). Browsers: log in."
        )
    }


def test_all_routes_pass_when_auth_disabled(
    auth_client: tuple[TestClient, MagicMock],
) -> None:
    client, auth_service = auth_client
    auth_service.enabled = False

    response = client.get("/api/tasks")

    assert response.status_code == 200
