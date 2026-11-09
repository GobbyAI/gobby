"""Inbox replay keeps each envelope's session binding without the machine bearer selecting one.

The machine bearer never carries a session selector. A replay whose envelope names a session
the daemon can find presents that session's own bound credential and keeps its selector.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from gobby.hooks.inbox import _post_envelope
from gobby.utils.local_token import (
    SESSION_TOKEN_KIND,
    AgentApiTokenClaims,
    classify_agent_api_token,
    derive_managed_signing_key,
)

pytestmark = pytest.mark.unit

OPERATOR_TOKEN = "operator-machine-token"
SESSION_ID = "5f1c0c3e-8a1e-4c1b-9d52-2d9b6c1f0a11"
PROJECT_ID = "project-1"
MACHINE_ID = "machine-1"
SELECTORS = (
    "X-Gobby-Session-Id",
    "X-Gobby-Agent-Run-Id",
    "X-Gobby-Managed-Execution-Id",
    "X-Gobby-Terminal-Context",
)


class _SessionRowDb:
    """Stands in for the hub database: one session row, found by its id and machine."""

    def __init__(self, row: dict[str, str] | None) -> None:
        self._row = row

    def fetchone(self, _sql: str, params: tuple[Any, ...]) -> dict[str, str] | None:
        # The lookup asks for this row's id on this row's machine, so a row from
        # another machine is never found.
        if self._row is not None and params == (self._row["id"], self._row["machine_id"]):
            return self._row
        return None


def _app_with_session(row: dict[str, str] | None) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(database=_SessionRowDb(row)))


def _envelope(headers: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "response_capability": "hook-response.v1",
        "hook_type": "pre-tool-use",
        "input_data": {"session_id": "provider-session", "tool_name": "Bash"},
        "source": "claude",
        "headers": headers,
    }


async def _replayed_headers(
    monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any], app: Any = None
) -> httpx.Headers:
    seen: list[httpx.Request] = []

    def backend(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient

    def replay_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(backend)
        return real_client(**kwargs)

    monkeypatch.setattr("gobby.hooks.inbox.httpx.AsyncClient", replay_client)
    with patch("gobby.hooks.inbox.read_local_api_token", return_value=OPERATOR_TOKEN):
        response = await _post_envelope(envelope, app=app)

    assert response.status_code == 200
    assert len(seen) == 1
    return seen[0].headers


async def test_replay_drops_session_selectors_it_would_present_with_the_machine_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await _replayed_headers(
        monkeypatch,
        _envelope(
            {
                "X-Gobby-Project-Id": "project-1",
                "X-Gobby-Session-Id": "provider-session",
                "X-Gobby-Agent-Run-Id": "run-1",
                "X-Gobby-Managed-Execution-Id": "execution-1",
                "X-Gobby-Terminal-Context": '{"parent_pid": 4242}',
            }
        ),
    )

    assert headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
    for name in SELECTORS:
        assert name not in headers, name
    assert headers["X-Gobby-Project-Id"] == "project-1"


async def test_replay_without_selectors_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = await _replayed_headers(monkeypatch, _envelope({"X-Gobby-Project-Id": "project-1"}))

    assert headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
    assert headers["X-Gobby-Project-Id"] == "project-1"


async def test_replay_presents_the_envelope_session_bound_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.hooks.inbox.get_machine_id", lambda: MACHINE_ID)
    app = _app_with_session({"id": SESSION_ID, "project_id": PROJECT_ID, "machine_id": MACHINE_ID})

    headers = await _replayed_headers(
        monkeypatch,
        _envelope(
            {
                "X-Gobby-Project-Id": PROJECT_ID,
                "X-Gobby-Session-Id": SESSION_ID,
                "X-Gobby-Agent-Run-Id": "run-1",
                "X-Gobby-Managed-Execution-Id": "execution-1",
                "X-Gobby-Terminal-Context": '{"parent_pid": 4242}',
            }
        ),
        app=app,
    )

    bearer = headers["Authorization"].removeprefix("Bearer ")
    assert bearer != OPERATOR_TOKEN
    claims = classify_agent_api_token(bearer, derive_managed_signing_key(OPERATOR_TOKEN))
    assert isinstance(claims, AgentApiTokenClaims)
    assert (claims.kind, claims.session_id, claims.project_id) == (
        SESSION_TOKEN_KIND,
        SESSION_ID,
        PROJECT_ID,
    )
    assert headers["X-Gobby-Session-Id"] == SESSION_ID
    assert headers["X-Gobby-Project-Id"] == PROJECT_ID
    for name in (
        "X-Gobby-Agent-Run-Id",
        "X-Gobby-Managed-Execution-Id",
        "X-Gobby-Terminal-Context",
    ):
        assert name not in headers, name


async def test_replay_without_a_session_row_stays_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = await _replayed_headers(
        monkeypatch,
        _envelope({"X-Gobby-Project-Id": PROJECT_ID, "X-Gobby-Session-Id": SESSION_ID}),
        app=_app_with_session(None),
    )

    assert headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
    assert "X-Gobby-Session-Id" not in headers
    assert headers["X-Gobby-Project-Id"] == PROJECT_ID


async def test_replay_does_not_bind_a_session_from_another_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.hooks.inbox.get_machine_id", lambda: MACHINE_ID)
    other_machine_row = {"id": SESSION_ID, "project_id": PROJECT_ID, "machine_id": "machine-2"}

    headers = await _replayed_headers(
        monkeypatch,
        _envelope({"X-Gobby-Project-Id": PROJECT_ID, "X-Gobby-Session-Id": SESSION_ID}),
        app=_app_with_session(other_machine_row),
    )

    assert headers["Authorization"] == f"Bearer {OPERATOR_TOKEN}"
    assert "X-Gobby-Session-Id" not in headers
