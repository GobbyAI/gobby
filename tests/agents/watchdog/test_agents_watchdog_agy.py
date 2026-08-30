import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TIMESTAMP = "2026-07-22T12:00:00+00:00"


def _record(
    source: str,
    record_type: str,
    *,
    content: str | None = "hello",
    thinking: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    timestamp: object = _TIMESTAMP,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": source,
        "type": record_type,
        "created_at": timestamp,
    }
    if content is not None:
        payload["content"] = content
    if thinking is not None:
        payload["thinking"] = thinking
    if tool_calls is not None:
        payload["tool_calls"] = tool_calls
    return payload


def _write(path: Path, records: list[dict[str, object] | str | bytes]) -> None:
    with path.open("wb") as handle:
        for record in records:
            if isinstance(record, bytes):
                raw = record
            elif isinstance(record, str):
                raw = record.encode()
            else:
                raw = json.dumps(record).encode()
            handle.write(raw + b"\n")


def _reader() -> Any:
    agy_watchdog: Any
    try:
        import gobby.agents.watchdog.agy as agy_watchdog
    except ImportError:
        agy_watchdog = None
    reader_cls = getattr(agy_watchdog, "AgyTranscriptWatchdogReader", None)
    assert reader_cls is not None
    return reader_cls()


@pytest.mark.asyncio
async def test_agy_reader_classifies_completed_turn_from_gate0_shapes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agy.jsonl"
    _write(
        path,
        [
            _record("USER_EXPLICIT", "USER_INPUT", content="do the work"),
            _record("MODEL", "PLANNER_RESPONSE", content="done"),
        ],
    )

    snapshot = await _reader().read(str(path))

    assert snapshot.provider == "agy"
    assert snapshot.latest_turn_kind == "completed"
    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.timestamp == datetime(2026, 7, 22, 12, tzinfo=UTC)
    assert snapshot.latest_activity_kind == "message"
    assert snapshot.latest_model_output_line_num == 2
    assert snapshot.has_conclusive_turn_completed is True
    assert snapshot.last_malformed_line_num is None


@pytest.mark.asyncio
async def test_agy_reader_classifies_thinking_and_tools(tmp_path: Path) -> None:
    path = tmp_path / "agy-tools.jsonl"
    _write(
        path,
        [
            _record("USER_EXPLICIT", "USER_INPUT", content="list files"),
            _record(
                "MODEL",
                "PLANNER_RESPONSE",
                content="",
                thinking="need ls",
                tool_calls=[{"name": "list_dir", "args": {"DirectoryPath": "/repo"}}],
            ),
            _record("MODEL", "TOOL_RESULT", content="ok"),
        ],
    )

    snapshot = await _reader().read(str(path))

    assert snapshot.provider == "agy"
    assert snapshot.latest_activity_kind == "tool"
    assert snapshot.latest_turn_kind == "started"
    assert snapshot.has_conclusive_turn_completed is False
    payload_types = [event.payload_type for event in snapshot.tail]
    assert "thinking" in payload_types or "tool_call" in payload_types
