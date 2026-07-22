import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.agents.watchdog.droid import DroidTranscriptWatchdogReader

pytestmark = pytest.mark.unit

_TIMESTAMP = "2026-07-22T12:00:00+00:00"
_SECRET = "watchdog-droid-secret"


def _message(
    role: str,
    blocks: list[dict[str, object]],
    *,
    timestamp: object = _TIMESTAMP,
) -> dict[str, object]:
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {"role": role, "content": blocks},
    }


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


@pytest.mark.asyncio
async def test_droid_reader_extracts_reasoning_activity_with_utc_timestamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "droid.jsonl"
    _write(
        path,
        [
            _message("user", [{"type": "text", "text": "prompt"}]),
            _message("assistant", [{"type": "thinking", "thinking": "private"}]),
        ],
    )

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "droid"
    assert snapshot.latest_activity_kind == "reasoning"
    assert snapshot.latest_model_output_line_num == 2
    assert snapshot.tail[-1].timestamp == datetime(2026, 7, 22, 12, tzinfo=UTC)
    assert snapshot.tail[-1].event_type == "assistant"
    assert snapshot.tail[-1].payload_type == "thinking"
    assert snapshot.latest_turn_event is None
    assert snapshot.provider_error_event is None
    assert snapshot.has_conclusive_turn_completed is False
    assert snapshot.has_conclusive_capacity_error is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ({"type": "text", "text": "reply"}, "message"),
        ({"type": "tool_use", "name": "Read", "input": {}}, "tool"),
        ({"type": "thinking", "thinking": "private"}, "reasoning"),
    ],
)
async def test_droid_assistant_activity_kind(
    tmp_path: Path,
    block: dict[str, object],
    expected: str,
) -> None:
    path = tmp_path / "activity.jsonl"
    _write(path, [_message("assistant", [block])])

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_activity_kind == expected
    assert snapshot.latest_model_output_line_num == 1


@pytest.mark.asyncio
async def test_droid_error_shaped_tool_result_and_session_end_stay_diagnostic_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "diagnostic.jsonl"
    _write(
        path,
        [
            {"type": "session_start"},
            _message(
                "user",
                [
                    {
                        "type": "tool_result",
                        "content": "failure detail",
                        "is_error": True,
                    }
                ],
            ),
            {"type": "session_end", "timestamp": _TIMESTAMP},
        ],
    )

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_event is None
    assert snapshot.provider_error_event is None
    assert snapshot.provider_error_kind is None
    assert snapshot.has_conclusive_turn_completed is False
    assert snapshot.has_conclusive_capacity_error is False


@pytest.mark.asyncio
async def test_droid_live_todo_state_shape_is_valid_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "todo-state.jsonl"
    _write(
        path,
        [{"type": "todo_state", "timestamp": _TIMESTAMP, "todos": {"todos": "[]"}}],
    )

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num is None
    assert snapshot.tail[-1].event_type == "system"
    assert snapshot.tail[-1].payload_type == "todo_state"
    assert snapshot.latest_activity_kind is None


@pytest.mark.asyncio
async def test_droid_tail_is_bounded_and_structurally_redacted(tmp_path: Path) -> None:
    path = tmp_path / "redacted.jsonl"
    records = [_message("assistant", [{"type": "text", "text": str(index)}]) for index in range(12)]
    records.extend(
        [
            {"type": _SECRET, "timestamp": _SECRET},
            _message(
                "assistant",
                [{"type": _SECRET, "value": _SECRET}],
                timestamp=_SECRET,
            ),
        ]
    )
    _write(path, records)

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))
    encoded = json.dumps(snapshot.to_log_dict())

    assert len(snapshot.tail) == 8
    assert snapshot.tail[-1].event_type == "assistant"
    assert snapshot.tail[-1].payload_type == "other"
    assert snapshot.tail[-1].timestamp is None
    assert _SECRET not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        b'{"type":"message"',
        b"\xff\xfe",
        b"[]",
    ],
)
async def test_droid_scanner_failures_poison_parse_confidence(
    tmp_path: Path,
    record: bytes,
) -> None:
    path = tmp_path / "scanner-failure.jsonl"
    _write(path, [record])

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 1
    assert snapshot.has_conclusive_turn_completed is False
    assert snapshot.has_conclusive_capacity_error is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        {"type": "message"},
        {"type": "message", "message": {"role": "assistant", "content": "bad"}},
        {"type": "todo_state", "todos": "bad"},
        {"type": 7},
    ],
)
async def test_droid_malformed_recognized_records_poison_parse_confidence(
    tmp_path: Path,
    record: dict[str, object],
) -> None:
    path = tmp_path / "malformed.jsonl"
    _write(path, [record])

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 1


@pytest.mark.asyncio
async def test_droid_unknown_record_and_blank_line_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "unknown.jsonl"
    _write(
        path,
        [
            _message("assistant", [{"type": "text", "text": "reply"}]),
            "   ",
            {"type": "future_record", "payload": {"value": "ignored"}},
        ],
    )

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num is None
    assert len(snapshot.tail) == 1


@pytest.mark.asyncio
async def test_droid_empty_transcript_returns_empty_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")

    snapshot = await DroidTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "droid"
    assert snapshot.tail == ()
    assert snapshot.latest_activity_kind is None
    assert json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_droid_reader_raises_oserror_for_missing_path(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await DroidTranscriptWatchdogReader().read(str(tmp_path / "missing.jsonl"))
