"""Hook ingress binds a key-authenticated envelope's machine to the key."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

HUB = "21000000-0000-4000-8000-0000000000b1"
KEY_MACHINE = "21000000-0000-4000-8000-0000000000b2"
OTHER = "21000000-0000-4000-8000-0000000000b3"
_SECRET = "hook-binding-secret"
_MISSING = object()


def _front_door(machine: str) -> dict[str, str]:
    return {
        "X-Gobby-Front-Door": _SECRET,
        "X-Gobby-User-Id": TEST_USER_ID,
        "X-Gobby-Machine-Id": machine,
        "X-Gobby-Key-Id": "hook-key",
    }


def _envelope(machine: object) -> dict[str, Any]:
    input_data: dict[str, Any] = {"session_id": "droid-123", "cwd": "/tmp"}
    if machine is not _MISSING:
        input_data["machine_id"] = machine
    return {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "hook_type": "PreToolUse",
        "source": "droid",
        "input_data": input_data,
    }


def _post(
    temp_db: HubDatabase, body: dict[str, Any], headers: dict[str, str]
) -> tuple[int, Any, MagicMock]:
    server = create_http_server(port=60887, test_mode=True, session_manager=SessionManager(temp_db))
    server.auth_service.bind_runtime(
        grant_service=None, local_machine_id=HUB, clock=None, front_door_secret=_SECRET
    )
    hook_manager = MagicMock()
    hook_manager.shutdown_async = AsyncMock()
    server.app.state.hook_manager = hook_manager
    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.droid.DroidAdapter") as adapter_cls,
    ):
        adapter_cls.return_value.handle_native.return_value = {"continue": True}
        response = client.post("/api/hooks/execute", json=body, headers=headers)
    return response.status_code, response.json(), adapter_cls


@pytest.mark.parametrize(
    ("envelope_machine", "accepted"),
    [(KEY_MACHINE, True), (OTHER, False), (HUB, False), (_MISSING, False), ("", False)],
)
def test_envelope_machine_must_match_key(
    temp_db: HubDatabase, envelope_machine: object, accepted: bool
) -> None:
    status, body, adapter_cls = _post(
        temp_db, _envelope(envelope_machine), _front_door(KEY_MACHINE)
    )

    if accepted:
        assert (status, body) == (200, {"continue": True})
        adapter_cls.return_value.handle_native.assert_called_once()
    else:
        assert (status, body) == (403, {"code": "hook_machine_mismatch"})
        adapter_cls.assert_not_called()


@pytest.mark.parametrize("envelope_machine", [OTHER, _MISSING])
def test_local_hook_unchanged(temp_db: HubDatabase, envelope_machine: object) -> None:
    status, body, adapter_cls = _post(temp_db, _envelope(envelope_machine), {})

    assert (status, body) == (200, {"continue": True})
    adapter_cls.return_value.handle_native.assert_called_once()
