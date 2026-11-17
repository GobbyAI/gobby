"""Hook attribution follows the live binding and the provider conversation."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

from gobby.servers.http import HTTPServer
from gobby.servers.routes.mcp.hooks import _run_bound_platform_session


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [("old-conversation", "origin"), ("current-conversation", "current"), (None, "current")],
)
def test_hook_binding_uses_conversation_without_clear_lineage(
    provider_id: str | None, expected: str
) -> None:
    claims = SimpleNamespace(agent_run_id="run", session_id="origin", project_id="project")
    sessions = MagicMock()
    sessions.get.return_value = SimpleNamespace(external_id="current-conversation")
    sessions.db.fetchone.return_value = None
    server = cast(
        HTTPServer,
        SimpleNamespace(
            auth_service=SimpleNamespace(verified_agent_claims=lambda _request: claims),
            session_manager=sessions,
        ),
    )
    request = Request({"type": "http", "headers": []})
    input_data = {} if provider_id is None else {"session_id": provider_id}

    with patch("gobby.servers.routes.mcp.hooks.current_run_session_id", return_value="current"):
        attributed = _run_bound_platform_session(server, request, input_data, "origin")

    assert attributed == expected
    sessions.db.fetchone.assert_not_called()
