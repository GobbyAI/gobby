"""Live-daemon coverage for the API key bootstrap lockout behind the gdaemon front door."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from tests.e2e.conftest import DaemonInstance

pytestmark = pytest.mark.e2e

_ALLOWED_FAILURES = 5
_BAD_BOOTSTRAP = {
    "email": "lockout-e2e@gobby.local",
    "password": "not-the-password",
    "machine_id": str(uuid4()),
    "label": "lockout e2e",
}


def _bootstrap(client: httpx.Client, host: str, port: int, forged_for: str | None) -> int:
    headers = {"X-Forwarded-For": forged_for} if forged_for else {}
    response = client.post(
        f"http://{host}:{port}/api/auth/keys/bootstrap", json=_BAD_BOOTSTRAP, headers=headers
    )
    return response.status_code


def test_forged_forwarding_cannot_reset_lockout(daemon_instance: DaemonInstance) -> None:
    port = daemon_instance.http_port
    with httpx.Client(timeout=10.0) as client:
        ipv4 = [
            _bootstrap(client, "127.0.0.1", port, f"203.0.113.{attempt}")
            for attempt in range(_ALLOWED_FAILURES + 3)
        ]
        ipv6 = _bootstrap(client, "[::1]", port, None)

    assert ipv4 == [401] * _ALLOWED_FAILURES + [429] * 3
    assert ipv6 == 401
