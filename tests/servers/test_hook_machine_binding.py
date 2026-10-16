"""Hook ingress binds the envelope's machine to the key, or to the hub without one."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.hooks.envelope_dedupe import ENVELOPE_ID_HEADER
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


@pytest.fixture(autouse=True)
def _hub_machine() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", HUB):
        yield


def _post(
    temp_db: HubDatabase,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    envelope_id: str | None = None,
) -> tuple[int, Any, MagicMock, MagicMock]:
    server = create_http_server(port=60887, test_mode=True, session_manager=SessionManager(temp_db))
    server.auth_service.bind_runtime(
        grant_service=None, local_machine_id=HUB, clock=None, front_door_secret=_SECRET
    )
    hook_manager = MagicMock()
    hook_manager.shutdown_async = AsyncMock()
    server.app.state.hook_manager = hook_manager
    request_headers = {**headers, ENVELOPE_ID_HEADER: envelope_id} if envelope_id else headers
    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.droid.DroidAdapter") as adapter_cls,
        patch("gobby.servers.routes.mcp.hooks.claim_envelope_processing") as claim,
    ):
        adapter_cls.return_value.handle_native.return_value = {"continue": True}
        response = client.post("/api/hooks/execute", json=body, headers=request_headers)
    return response.status_code, response.json(), adapter_cls, claim


def _assert_refused(status: int, body: Any, adapter_cls: MagicMock, claim: MagicMock) -> None:
    assert (status, body) == (403, {"code": "hook_machine_mismatch"})
    claim.assert_not_called()
    adapter_cls.assert_not_called()


@pytest.mark.parametrize(
    ("envelope_machine", "accepted"),
    [(KEY_MACHINE, True), (OTHER, False), (HUB, False), (_MISSING, False), ("", False)],
)
def test_envelope_machine_must_match_key(
    temp_db: HubDatabase, envelope_machine: object, accepted: bool
) -> None:
    if accepted:
        status, body, adapter_cls, _claim = _post(
            temp_db, _envelope(envelope_machine), _front_door(KEY_MACHINE)
        )
        assert (status, body) == (200, {"continue": True})
        adapter_cls.return_value.handle_native.assert_called_once()
    else:
        _assert_refused(
            *_post(
                temp_db,
                _envelope(envelope_machine),
                _front_door(KEY_MACHINE),
                envelope_id="refused-key-envelope",
            )
        )


@pytest.mark.parametrize(
    ("envelope_machine", "accepted"),
    [(HUB, True), (OTHER, False), (KEY_MACHINE, False), (_MISSING, False), ("", False)],
)
def test_unverified_hook_must_name_hub_machine(
    temp_db: HubDatabase, envelope_machine: object, accepted: bool
) -> None:
    if accepted:
        status, body, adapter_cls, _claim = _post(temp_db, _envelope(envelope_machine), {})
        assert (status, body) == (200, {"continue": True})
        adapter_cls.return_value.handle_native.assert_called_once()
    else:
        _assert_refused(
            *_post(
                temp_db,
                _envelope(envelope_machine),
                {},
                envelope_id="refused-local-envelope",
            )
        )
