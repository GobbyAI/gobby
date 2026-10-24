"""Browser testing cookies have process-local, short-lived authority."""

import base64
import json
from unittest.mock import patch

import pytest

from gobby.servers.browser_sessions import PREFIX, BrowserSessions, browser_route_allowed
from gobby.utils.local_token import AgentApiTokenClaims


def test_browser_cookie_provenance_expiry_logout_and_restart() -> None:
    sessions = BrowserSessions()
    claims = AgentApiTokenClaims("session", "project", "machine", 1000, 10000, agent_run_id="run")
    with patch("gobby.servers.browser_sessions.time.time", return_value=1000):
        token = sessions.create("user", claims)
        other = sessions.create("user", claims)
        session = sessions.get(token)
        assert session is not None and session.claims == claims
        payload = token.removeprefix(PREFIX).split(".", 1)[0]
        provenance = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert provenance["kind"] == "browser-testing"
        assert provenance["project_id"] == "project"
        assert provenance["run_id"] == "run"
        assert provenance["exp"] == 4600
        assert BrowserSessions().get(token) is None
        sessions.revoke(token)
        assert sessions.get(token) is None
        assert sessions.get(other) is not None
    with patch("gobby.servers.browser_sessions.time.time", return_value=4600):
        assert sessions.get(other) is None


@pytest.mark.parametrize("token", [PREFIX + "body.é", PREFIX + "body.bad", PREFIX + "x" * 8192])
def test_malformed_browser_cookie_is_refused(token: str) -> None:
    assert BrowserSessions().get(token) is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/mcp/servers",
        "/api/mcp/call",
        "/api/agents/spawn",
        "/api/pipelines/run",
        "/api/cron/jobs",
        "/api/skills/install",
        "/api/rules/import",
        "/api/hooks/execute",
        "/api/config",
        "/api/secrets",
        "/api/bootstrap",
        "/api/settings",
        "/api/users",
        "/api/auth/password",
        "/api/projects/register",
        "/api/projects/own/checkouts",
        "/api/unknown-future-action",
    ],
)
def test_browser_mutations_are_denied_by_default(method: str, path: str) -> None:
    assert browser_route_allowed(method, path) is False
