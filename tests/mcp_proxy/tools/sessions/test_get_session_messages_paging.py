"""get_session_messages pages rendered groups and reports its total in the same unit (#23295)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.sessions.transcript_index import clear_index_cache
from gobby.sessions.transcript_reader import TranscriptReader

pytestmark = pytest.mark.unit

SESSION_ID = "23295000-0000-4000-8000-000000000001"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"
TURNS = 6


def _turn(index: int) -> list[dict[str, Any]]:
    tool_id = f"toolu_{index:04d}"
    return [
        {"type": "user", "message": {"role": "user", "content": f"question {index}"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"n": index}}
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"answer {index}"}],
            },
        },
    ]


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(
        "gobby.sessions.transcript_reader.require_local_session_ownership",
        lambda session: LOCAL_MACHINE_ID,
    )
    transcript = tmp_path / "transcript.jsonl"
    records = [line for index in range(TURNS) for line in _turn(index)]
    for second, record in enumerate(records):
        record["timestamp"] = f"2026-10-01T12:00:{second:02d}Z"
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    session = MagicMock()
    session.id = SESSION_ID
    session.machine_id = LOCAL_MACHINE_ID
    session.external_id = "no-archive"
    session.source = "claude"
    session.transcript_path = str(transcript)
    session_manager = MagicMock()
    session_manager.get.return_value = session
    session_manager.resolve_session_reference.return_value = SESSION_ID
    clear_index_cache()
    reader = TranscriptReader(session_manager)
    yield create_session_messages_registry(
        session_manager=session_manager, transcript_reader=reader
    )
    clear_index_cache()


async def test_total_count_pages_to_the_last_rendered_group(registry: Any) -> None:
    limit = 2
    first = await registry.call(
        "get_session_messages", {"session_id": SESSION_ID, "limit": limit, "offset": 0}
    )
    assert first["success"] is True, first
    total = first["total_count"]

    reachable: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await registry.call(
            "get_session_messages", {"session_id": SESSION_ID, "limit": limit, "offset": offset}
        )
        if not page["messages"]:
            break
        reachable.extend(page["messages"])
        offset += page["returned_count"]

    tail = await registry.call(
        "get_session_messages",
        {"session_id": SESSION_ID, "limit": limit, "offset": total - limit},
    )

    # Tool calls and results merge into their turn, so groups < parsed records.
    assert len(reachable) < len(_turn(0)) * TURNS
    assert total == len(reachable)
    assert tail["messages"] == reachable[-limit:]
    assert f"answer {TURNS - 1}" in json.dumps(tail["messages"][-1])


async def test_limit_above_cap_returns_the_effective_limit(
    registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    every = await registry.call(
        "get_session_messages", {"session_id": SESSION_ID, "limit": 50, "offset": 0}
    )
    cap = 2
    monkeypatch.setattr("gobby.mcp_proxy.tools.sessions._messages.RENDERED_LIMIT_MAX", cap)
    first = await registry.call(
        "get_session_messages", {"session_id": SESSION_ID, "limit": cap + 1, "offset": 0}
    )
    assert first["success"] is True, first
    assert first["returned_count"] == cap
    assert first["limit"] == cap

    total = first["total_count"]
    assert total == len(every["messages"])

    tail = await registry.call(
        "get_session_messages",
        {"session_id": SESSION_ID, "limit": cap + 1, "offset": total - first["limit"]},
    )
    assert tail["messages"] == every["messages"][-cap:]
