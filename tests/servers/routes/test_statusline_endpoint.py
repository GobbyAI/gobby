"""Regression coverage for the retired statusline usage endpoint."""

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.grant_auth import AuthDecision
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.servers.routes.sessions import create_sessions_router

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


@pytest.mark.parametrize(
    ("authenticated", "expected_status"),
    [(True, 404), (False, 401)],
)
def test_statusline_usage_endpoint_is_removed(
    authenticated: bool,
    expected_status: int,
) -> None:
    server = MagicMock()
    server.auth_service.enabled = True
    # AuthMiddleware reads the decision from authenticate(); a MagicMock's
    # attribute would answer every question truthily, including the shutdown
    # gate, and turn both cases into a 503.
    server.auth_service.authenticate.return_value = AuthDecision(allowed=authenticated)
    server.services.http_admission_closed = False
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))

    app = FastAPI()
    app.include_router(create_sessions_router(server))
    app.state.hook_manager = MagicMock()
    app.add_middleware(AuthMiddleware, server=cast("HTTPServer", server))

    assert "/api/sessions/statusline" not in app.openapi()["paths"]

    response = TestClient(app).post(
        "/api/sessions/statusline",
        json={"session_id": "retired-statusline-writer"},
    )

    assert response.status_code == expected_status
    server.session_manager.update_usage.assert_not_called()
