"""Distinguish a routing refusal from uncertain command delivery."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit
MACHINE = "d6035e97-1c72-4d8c-a319-34bb22aeb787"


@pytest.mark.parametrize(
    ("body", "expected", "error"),
    [
        ({"ok": True, "result": {"accepted": True}}, {"accepted": True}, None),
        ({"ok": True, "result": {"stopped": False}}, {"stopped": False}, None),
        ({"ok": True, "result": {}}, {}, None),
        (
            {"ok": False, "error": {"code": "node_offline", "message": "offline", "stage": "none"}},
            None,
            ("node_offline", "offline", "none"),
        ),
        (
            {
                "ok": False,
                "error": {"code": "spawn_failed", "message": "started", "stage": "partial"},
            },
            None,
            ("spawn_failed", "started", "partial"),
        ),
        (
            {"ok": False, "error": {"code": "node_timeout", "message": "late", "stage": None}},
            None,
            ("node_timeout", "late", None),
        ),
    ],
)
async def test_send_maps_replies(
    body: dict[str, Any], expected: dict[str, Any] | None, error: tuple[str, str, str | None] | None
) -> None:
    from gobby.hub.node_client import NodeCommandClient, NodeCommandError

    requests: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"type": "reply", "v": 2, "request_id": "r1", **body})

    client = NodeCommandClient(
        port=60887, secret="private-secret", transport=httpx.MockTransport(route)
    )
    payload = {"agent_run_id": "run", "text": "hello", "delay_ms": 0}
    if error is None:
        assert await client.send(MACHINE, "write", payload, 1200) == expected
    else:
        with pytest.raises(NodeCommandError) as caught:
            await client.send(MACHINE, "write", payload, 1200)
        assert (caught.value.code, str(caught.value), caught.value.stage) == error
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"http://127.0.0.1:60887/api/internal/nodes/{MACHINE}/commands"
    assert request.method == "POST"
    assert request.headers["X-Gobby-Backend-Secret"] == "private-secret"
    assert json.loads(request.content) == {
        "command": "write",
        "payload": payload,
        "timeout_ms": 1200,
    }
    assert request.extensions["timeout"]["read"] == pytest.approx(6.2)


@pytest.mark.parametrize("failure", ["no_secret", "404", "connect", "connect_timeout"])
async def test_unavailable_routing_is_typed(failure: str) -> None:
    from gobby.hub.node_client import NodeCommandClient, NodeCommandError

    requests: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        if failure == "connect_timeout":
            raise httpx.ConnectTimeout("unreachable", request=request)
        return httpx.Response(404)

    client = NodeCommandClient(
        port=60887,
        secret=None if failure == "no_secret" else "secret",
        transport=httpx.MockTransport(route),
    )
    with pytest.raises(NodeCommandError) as caught:
        await client.send(MACHINE, "cancel", {"agent_run_id": "run"}, 100)
    assert caught.value.code == "node_routing_unavailable"
    assert caught.value.stage == "none"
    assert len(requests) == (0 if failure == "no_secret" else 1)


@pytest.mark.parametrize(
    "failure", ["500", "malformed", "truncated", "invalid_frame", "read_timeout", "reset"]
)
async def test_post_send_failures_are_uncertain(failure: str) -> None:
    from gobby.hub.node_client import NodeCommandClient, NodeCommandError

    accepted: list[bytes] = []

    def route(request: httpx.Request) -> httpx.Response:
        accepted.append(request.content)
        if failure == "read_timeout":
            raise httpx.ReadTimeout("no reply", request=request)
        if failure == "reset":
            raise httpx.ReadError("connection reset", request=request)
        if failure == "500":
            return httpx.Response(500)
        if failure == "invalid_frame":
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(200, content=b'{"type":' if failure == "truncated" else b"not json")

    client = NodeCommandClient(port=60887, secret="secret", transport=httpx.MockTransport(route))
    with pytest.raises(NodeCommandError) as caught:
        await client.send(MACHINE, "cancel", {"agent_run_id": "run"}, 100)
    assert len(accepted) == 1
    assert caught.value.code == "node_reply_lost"
    assert caught.value.stage is None


@pytest.mark.parametrize(
    "failure", [None, "no_secret", "404", "500", "malformed", "wrong_ids", "timeout"]
)
async def test_connected_machine_ids(failure: str | None) -> None:
    from gobby.hub.node_client import NodeCommandClient, NodeCommandError

    requests: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("late", request=request)
        if failure in {"404", "500"}:
            return httpx.Response(int(failure))
        if failure == "malformed":
            return httpx.Response(200, content=b"{")
        return httpx.Response(
            200, json={"machine_ids": [3] if failure == "wrong_ids" else [MACHINE]}
        )

    client = NodeCommandClient(
        port=60887,
        secret=None if failure == "no_secret" else "secret",
        transport=httpx.MockTransport(route),
    )
    if failure is None:
        assert await client.connected_machine_ids() == {MACHINE}
        assert requests[0].method == "GET"
        assert requests[0].url.path == "/api/internal/nodes/connected"
        assert requests[0].headers["X-Gobby-Backend-Secret"] == "secret"
    else:
        with pytest.raises(NodeCommandError) as caught:
            await client.connected_machine_ids()
        assert caught.value.code == "node_routing_unavailable"
        assert caught.value.stage == "none"
        assert len(requests) == (0 if failure == "no_secret" else 1)


def test_environment_and_cached_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import gobby.hub.node_client as module

    monkeypatch.setenv("GOBBY_FRONT_DOOR_SECRET", "env-secret")
    monkeypatch.setattr(module, "load_bootstrap", lambda: SimpleNamespace(daemon_port=61234))
    module.node_command_client.cache_clear()
    try:
        first = module.node_command_client()
        assert first is module.node_command_client()
        assert first.port == 61234
        assert first.secret == "env-secret"
    finally:
        module.node_command_client.cache_clear()
