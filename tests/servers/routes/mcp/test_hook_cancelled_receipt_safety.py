"""A cancelled hook evaluation cannot leave an inbox delivery receipt pending.

#22706: the BEFORE_AGENT evaluation warning fires on a runtime ``run()``
cancellation, and a cancelled evaluation used to be suspected of stranding an
inbox delivery receipt. The receipt sweep runs at the top of ``/api/hooks/execute``
before any evaluation, so the ack is consumed and the message is marked delivered
regardless of what happens to the adapter afterwards.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from gobby.hooks.envelope_dedupe import ENVELOPE_ID_HEADER
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.servers.routes.mcp import hooks as hooks_route
from gobby.storage.hook_receipts import prepare_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

ENVELOPE_ID = "n-0000000000001-cancelled-receipt"
# The pending receipt belongs to an earlier delivered envelope, not the cancelled
# request envelope, exactly as a redelivery ack sits in the inbox.
ORIGINAL_ENVELOPE_ID = "n-0000000000000-original-delivery"
SESSION_ID = "5b5b5b5b-5b5b-4b5b-8b5b-5b5b5b5b5b5b"
PROJECT_ID = "7c7c7c7c-7c7c-4c7c-8c7c-7c7c7c7c7c7c"
MESSAGE_ID = "msg-cancelled-receipt"


def _envelope() -> dict[str, Any]:
    return {
        "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "hook_type": "BeforeAgent",
        "source": "claude",
        "input_data": {"prompt": "continue"},
    }


def _receipt_ack_envelope(receipt_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "delivery-receipt",
        "enqueued_at": "2026-04-16T12:00:00Z",
        "receipt_id": receipt_id,
        "original_envelope_id": ORIGINAL_ENVELOPE_ID,
        "delivery_generation": 1,
    }


@pytest.fixture
def inbox_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    directory = gobby_home / "hooks" / "inbox"
    directory.mkdir(parents=True)
    return directory


@pytest.mark.asyncio
async def test_cancelled_before_agent_consumes_the_pending_receipt(
    hub_db: HubDatabase,
    inbox_dir: Path,
) -> None:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "cancelled-receipt-safety"),
    )
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, session_type) "
        "VALUES (%s, %s, %s, %s, %s, 'terminal')",
        (
            SESSION_ID,
            SESSION_ID,
            "21000000-0000-4000-8000-000000000042",
            "claude",
            PROJECT_ID,
        ),
    )
    receipt = prepare_receipt(
        hub_db,
        session_id=SESSION_ID,
        envelope_id=ORIGINAL_ENVELOPE_ID,
        staged_payload={
            "pending_message_ids": [MESSAGE_ID],
            "pending_message_session_id": SESSION_ID,
        },
    )
    ack_path = inbox_dir / f"{ORIGINAL_ENVELOPE_ID}-ack.json"
    ack_path.write_text(json.dumps(_receipt_ack_envelope(receipt.receipt_id)), encoding="utf-8")

    server = create_http_server(port=60888, test_mode=True, session_manager=SessionManager(hub_db))
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    started = asyncio.Event()

    async def cancelled_adapter(*_args: object, **_kwargs: object) -> dict[str, Any]:
        # The sweep has already run at this point; the evaluation is then cancelled
        # mid-flight exactly as the _handle_cancelled warning path describes.
        started.set()
        await asyncio.Event().wait()
        return {"continue": True}

    with patch.object(hooks_route, "_run_adapter_hook", cancelled_adapter):
        async with server.app.router.lifespan_context(server.app):
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
                # The evaluation task is cancelled mid-flight, before it returns.
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    # The entry sweep consumed the ack before the cancelled evaluation ran.
    assert not ack_path.exists()
    # The CAS landed on the exact generation, so the staged delivery is terminal.
    row = hub_db.fetchone(
        "SELECT state FROM hook_receipt_effects WHERE receipt_id = %s",
        (receipt.receipt_id,),
    )
    assert row is not None and row["state"] == "acknowledged"
