"""An envelope processing lease dies with the hook execution that holds it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from gobby.hooks import adapter_execution
from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    claim_envelope_processing,
    read_envelope_marker,
)
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.servers.routes.mcp import hooks as hooks_route
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

ENVELOPE_ID = "n-0000000000001-lease-release"


def _envelope() -> dict[str, Any]:
    return {
        "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "hook_type": "session-start",
        "source": "claude",
        "input_data": {},
    }


@pytest.fixture
def processed_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    return gobby_home / "hooks" / "inbox" / "processed"


@pytest.fixture
def renewal_tasks(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []

    def start(envelope_id: str, owner_token: str) -> asyncio.Task[None]:
        task = adapter_execution.start_envelope_lease_renewal(envelope_id, owner_token)
        tasks.append(task)
        return task

    monkeypatch.setattr(hooks_route, "start_envelope_lease_renewal", start)
    return tasks


def _server(session_storage: SessionManager) -> Any:
    server = create_http_server(port=60887, test_mode=True, session_manager=session_storage)
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()
    return server


@pytest.mark.asyncio
async def test_cancelled_execution_stops_renewal_and_releases_the_claim(
    session_storage: SessionManager,
    processed_dir: Path,
    renewal_tasks: list[asyncio.Task[None]],
) -> None:
    server = _server(session_storage)
    started = asyncio.Event()

    async def stalled_adapter(*args: object, **kwargs: object) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {"continue": True}

    async with server.app.router.lifespan_context(server.app):
        with patch.object(hooks_route, "_run_adapter_hook", stalled_adapter):
            async with AsyncClient(
                transport=ASGITransport(app=server.app), base_url="http://test"
            ) as client:
                request = asyncio.create_task(
                    client.post(
                        "/api/hooks/execute",
                        headers={ENVELOPE_ID_HEADER: ENVELOPE_ID},
                        json=_envelope(),
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=2)
                assert read_envelope_marker(ENVELOPE_ID, processed_dir=processed_dir) is not None
                assert len(renewal_tasks) == 1
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    await asyncio.gather(*renewal_tasks, return_exceptions=True)
    assert renewal_tasks[0].cancelled()
    # The claim went with the execution: a replay can claim the envelope again.
    assert read_envelope_marker(ENVELOPE_ID, processed_dir=processed_dir) is None
    assert claim_envelope_processing(ENVELOPE_ID, processed_dir=processed_dir) is True


def test_finalized_marker_survives_request_teardown(
    session_storage: SessionManager,
    processed_dir: Path,
    renewal_tasks: list[asyncio.Task[None]],
) -> None:
    server = _server(session_storage)
    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter = MagicMock()
        adapter.handle_native.return_value = {"continue": True}
        adapter_cls.return_value = adapter
        response = client.post(
            "/api/hooks/execute",
            headers={ENVELOPE_ID_HEADER: ENVELOPE_ID},
            json=_envelope(),
        )

    assert response.status_code == 200
    record = read_envelope_marker(ENVELOPE_ID, processed_dir=processed_dir)
    assert record is not None
    assert record["status"] == "processed"
    assert len(renewal_tasks) == 1
    assert renewal_tasks[0].done()
    # Processed is terminal: the envelope cannot be claimed for a second run.
    assert claim_envelope_processing(ENVELOPE_ID, processed_dir=processed_dir) is False
