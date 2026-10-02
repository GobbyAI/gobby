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


def test_forged_forwarding_cannot_reset_lockout(daemon_instance: DaemonInstance) -> None:
    url = f"http://127.0.0.1:{daemon_instance.http_port}/api/auth/keys/bootstrap"
    with httpx.Client(timeout=10.0) as client:
        statuses = [
            client.post(
                url, json=_BAD_BOOTSTRAP, headers={"X-Forwarded-For": f"203.0.113.{attempt}"}
            ).status_code
            for attempt in range(_ALLOWED_FAILURES + 3)
        ]

    assert statuses == [401] * _ALLOWED_FAILURES + [429] * 3
