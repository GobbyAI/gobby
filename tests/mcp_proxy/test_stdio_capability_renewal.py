"""A seat whose capability expired retries its original call once the daemon renews it."""

import time
from pathlib import Path

import httpx
import pytest

from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.runtime_grants.launch import materialize_managed_launch
from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal
from gobby.utils.local_token import read_managed_api_token

pytestmark = pytest.mark.unit

SIGNING_KEY = b"expired-test-key"


async def test_renewed_capability_retries_original_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = Path(__file__).parents[1] / "runtime_grants/golden/direct_datastores.json"
    grant = GrantBundle.model_validate_json(golden.read_bytes()).model_copy(
        update={
            "principal": GrantPrincipal(
                kind="agent_run",
                machine_id="test-machine",
                project_id="test-project",
                execution_id="92c326c1-b3af-439a-88de-7f5dc935b359",
                session_id="a6d89fa4-d781-40f8-b7c4-2188bb24cf48",
            )
        }
    )
    launched_at = time.time() - 7200
    with monkeypatch.context() as clock:
        clock.setattr("gobby.utils.local_token.time.time", lambda: launched_at)
        expired = materialize_managed_launch(
            grant,
            dest_dir=tmp_path,
            signing_key=SIGNING_KEY,
            deadline_seconds=3600,
        )
    expired_token = read_managed_api_token(expired.env)
    assert expired_token is not None
    for key, value in expired.env.items():
        if key.startswith("GOBBY_"):
            monkeypatch.setenv(key, value)

    def daemon(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/runtime/handshake"):
            # The daemon rewrites the seat's grant file with a fresh capability.
            materialize_managed_launch(
                grant,
                dest_dir=tmp_path,
                signing_key=SIGNING_KEY,
                deadline_seconds=3600,
            )
            return httpx.Response(200, json={"grant": {}, "fencing_epoch": 1})
        if request.headers.get("Authorization") == f"Bearer {expired_token}":
            return httpx.Response(
                401,
                json={"error_code": "capability_expired", "error": "capability expired"},
            )
        return httpx.Response(200, json={"ok": True})

    proxy = DaemonProxy(60887)
    async with httpx.AsyncClient(transport=httpx.MockTransport(daemon)) as client:
        proxy._client = client
        result = await proxy._request("GET", "/api/admin/status")

    assert result.get("error_code") != "capability_expired"
    assert result.get("ok") is True
