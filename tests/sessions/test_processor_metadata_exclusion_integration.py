"""Integration: the live SessionMessageProcessor JSONL ingest path.

Covers #17418's live-path behavior, which the manual smoke test could not reach
(HTTP /api/sessions/register writes the session row but does NOT engage the
processor poll loop, so only the on-demand read path was exercised there). Here
we drive ``_process_session`` directly with a store-backed processor and assert:

1. Session stats exclude session metadata (the unmodeled-record sentinel and the
   native ai-title) -- only the assistant message counts.
2. The genuinely-unknown record persists exactly one ``unmodeled_observations``
   block_type row via the appender's store-backed ObservationTracker.
3. No rendered ``session_message`` broadcast carries a metadata group.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.unmodeled_observations import UnmodeledObservationStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000005"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


_ASSISTANT = (
    '{"type":"assistant","message":{"content":[{"type":"text","text":"live hello 17418"}]},'
    '"timestamp":"2026-06-27T12:00:00Z","uuid":"a1"}'
)
_UNKNOWN = (
    '{"type":"live-unknown-envelope-17418","detail":"fabricated",'
    '"timestamp":"2026-06-27T12:00:01Z","uuid":"u1"}'
)
_TITLE = (
    '{"type":"ai-title","aiTitle":"Live Title 17418",'
    '"timestamp":"2026-06-27T12:00:02Z","uuid":"t1"}'
)


async def test_live_processor_excludes_metadata_and_persists_observation(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    tmp_path: Path,
) -> None:
    tx = tmp_path / "live.jsonl"
    session = session_manager.register(
        external_id="proc-17418",
        machine_id="21000000-0000-4000-8000-000000000005",
        source="claude",
        project_id=None,
        transcript_path=str(tx),
    )
    sid = session.id

    processor = SessionMessageProcessor(
        temp_db, session_manager=session_manager, websocket_server=MagicMock()
    )
    # Register before the file exists (real Codex-style flow: register on
    # session_start, the rollout appears a beat later), then write it so the
    # poll reads from byte zero.
    processor.register_session(sid, str(tx), source="claude")
    tx.write_text("\n".join([_ASSISTANT, _UNKNOWN, _TITLE]) + "\n", encoding="utf-8")

    broadcasts: list[dict[str, Any]] = []

    async def _capture(session_id: str, payload: dict[str, Any], *, complete: bool) -> None:
        broadcasts.append(payload)

    processor._broadcast_rendered_session_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=_capture
    )

    await processor._process_session(sid, str(tx))

    # (1) Live stats exclude the sentinel + title: only the assistant counts.
    refreshed = session_manager.get(sid)
    assert refreshed is not None
    assert refreshed.message_count == 1
    assert refreshed.turn_count == 1

    # (2) The unknown record persisted exactly one block_type observation via the
    #     appender's store-backed ObservationTracker (processor_lifecycle wires
    #     ObservationTracker(self._observation_store) into the appender).
    store = UnmodeledObservationStore(temp_db)
    rows = [
        r
        for r in store.list_observations(source="claude", kind="block_type")
        if r.name == "live-unknown-envelope-17418"
    ]
    assert len(rows) == 1
    assert rows[0].count == 1

    # (3) No broadcast carries a metadata group; the assistant text WAS broadcast.
    broadcast_block_types = {
        block.get("type")
        for payload in broadcasts
        for block in (payload.get("content_blocks") or [])
    }
    assert "unknown" not in broadcast_block_types
    assert "session_title" not in broadcast_block_types
    assert "unmodeled_record" not in broadcast_block_types
    assert any(payload.get("role") == "assistant" for payload in broadcasts)
