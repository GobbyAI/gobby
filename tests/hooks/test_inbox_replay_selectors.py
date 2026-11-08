"""Inbox replay speaks as the machine caller, so it never carries a session selector."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from gobby.hooks.inbox import _post_envelope

pytestmark = pytest.mark.unit

OPERATOR_TOKEN = "operator-machine-token"
SELECTORS = (
    "X-Gobby-Session-Id",
    "X-Gobby-Agent-Run-Id",
    "X-Gobby-Managed-Execution-Id",
    "X-Gobby-Terminal-Context",
)


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
    monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any]
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
        response = await _post_envelope(envelope)

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
