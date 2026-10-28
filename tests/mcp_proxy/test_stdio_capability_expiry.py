"""An expired managed capability must initiate recovery and report a typed failure."""

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.runtime_grants.launch import materialize_managed_launch
from gobby.runtime_grants.schema import GrantBundle, GrantPrincipal

pytestmark = pytest.mark.unit


@pytest.fixture
async def expired_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], list[str]]:
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
        launch = materialize_managed_launch(
            grant,
            dest_dir=tmp_path,
            signing_key=b"expired-test-key",
            deadline_seconds=3600,
        )
    for key, value in launch.env.items():
        if key.startswith("GOBBY_"):
            monkeypatch.setenv(key, value)
    paths: list[str] = []

    def daemon(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.startswith("/api/runtime/handshake"):
            return httpx.Response(
                503,
                json={"error_code": "credential_issuance_failed", "error": "pool timeout"},
            )
        return httpx.Response(
            401,
            json={"error_code": "capability_expired", "error": "capability expired"},
        )

    proxy = DaemonProxy(60887)
    async with httpx.AsyncClient(transport=httpx.MockTransport(daemon)) as client:
        proxy._client = client
        result = await proxy._request("GET", "/api/admin/status")
    return result, paths


@pytest.mark.asyncio
async def test_expired_capability_initiates_rehandshake(
    expired_request: tuple[dict[str, Any], list[str]],
) -> None:
    _, paths = expired_request

    assert any(path.startswith("/api/runtime/handshake") for path in paths), (
        "capability_expired only replayed the expired token instead of re-handshaking"
    )


@pytest.mark.asyncio
async def test_failed_expiry_recovery_returns_typed_blocker(
    expired_request: tuple[dict[str, Any], list[str]],
) -> None:
    result, _ = expired_request
    assert result.get("success") is False
    assert result.get("error_code") == "capability_expired", result
    assert "expir" in str(result.get("error", "")).lower()
