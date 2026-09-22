"""CLI forwarding for explicit terminal termination."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from gobby.cli.sessions import sessions


def test_sessions_terminate_forwards_root_session_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_module = importlib.import_module("gobby.cli.sessions")
    response = MagicMock()
    response.json.return_value = {
        "success": True,
        "result": {"success": True, "terminal_id": "terminal-1", "state": "exited"},
    }
    calls: list[tuple[str, dict[str, object]]] = []

    def post(url: str, **kwargs: object) -> MagicMock:
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(sessions_module, "get_daemon_url", lambda: "http://daemon.test")
    monkeypatch.setattr(
        sessions_module, "daemon_auth_headers", lambda: {"Authorization": "Bearer test"}
    )
    monkeypatch.setattr(sessions_module.httpx, "post", post)

    result = CliRunner().invoke(sessions, ["terminate-terminal", "root-session", "--json"])

    assert result.exit_code == 0, result.output
    assert '"terminal_id": "terminal-1"' in result.output
    assert calls == [
        (
            "http://daemon.test/api/mcp/gobby-sessions/tools/terminate_terminal",
            {
                "json": {"reference": "root-session"},
                "headers": {"Authorization": "Bearer test"},
                "timeout": 30.0,
            },
        )
    ]
