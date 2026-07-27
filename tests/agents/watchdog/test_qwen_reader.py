import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.agents.watchdog.qwen import QwenTranscriptWatchdogReader

pytestmark = pytest.mark.unit

_TIMESTAMP = "2026-07-22T12:00:00+00:00"
_SECRET = "watchdog-qwen-secret"


def _record(
    record_type: str,
    parts: list[dict[str, object]],
    *,
    timestamp: object = _TIMESTAMP,
) -> dict[str, object]:
    return {
        "type": record_type,
        "timestamp": timestamp,
        "message": {"role": record_type, "parts": parts},
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
async def test_qwen_reader_extracts_reasoning_activity_with_utc_timestamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "qwen.jsonl"
    _write(
        path,
        [
            _record("user", [{"text": "prompt"}]),
            _record("assistant", [{"text": "private", "thought": True}]),
        ],
    )

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "qwen"
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
    ("record_type", "part", "expected", "model_output_line"),
    [
        ("assistant", {"text": "reply"}, "message", 1),
        ("assistant", {"text": "private", "thought": True}, "reasoning", 1),
        ("assistant", {"functionCall": {"name": "read", "args": {}}}, "tool", 1),
        (
            "tool_result",
            {"functionResponse": {"name": "read", "response": {}}},
            "tool",
            None,
        ),
        ("user", {"text": "prompt"}, "user_input", None),
    ],
)
async def test_qwen_activity_kind(
    tmp_path: Path,
    record_type: str,
    part: dict[str, object],
    expected: str,
    model_output_line: int | None,
) -> None:
    path = tmp_path / "activity.jsonl"
    _write(path, [_record(record_type, [part])])

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_activity_kind == expected
    assert snapshot.latest_model_output_line_num == model_output_line


@pytest.mark.asyncio
async def test_qwen_system_diagnostics_never_become_turn_or_error_signals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "system.jsonl"
    _write(
        path,
        [
            {"type": "system", "subtype": "ui_telemetry", "timestamp": _TIMESTAMP},
            {
                "type": "system",
                "subtype": "file_history_snapshot",
                "timestamp": _TIMESTAMP,
            },
            {"type": "system", "subtype": "future_system", "timestamp": _TIMESTAMP},
        ],
    )

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert [event.payload_type for event in snapshot.tail] == ["telemetry", "snapshot"]
    assert snapshot.latest_turn_event is None
    assert snapshot.provider_error_event is None
    assert snapshot.has_conclusive_turn_completed is False
    assert snapshot.has_conclusive_capacity_error is False


@pytest.mark.asyncio
async def test_qwen_tool_result_preserves_record_event_type(tmp_path: Path) -> None:
    path = tmp_path / "tool-result.jsonl"
    _write(path, [_record("tool_result", [{"type": "text", "text": "result"}])])

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert snapshot.tail[0].event_type == "tool_result"


@pytest.mark.asyncio
async def test_qwen_tail_is_bounded_and_structurally_redacted(tmp_path: Path) -> None:
    path = tmp_path / "redacted.jsonl"
    records = [_record("assistant", [{"text": str(index)}]) for index in range(12)]
    records.extend(
        [
            {"type": _SECRET, "timestamp": _SECRET},
            {"type": "system", "subtype": _SECRET, "timestamp": _SECRET},
            _record(
                "assistant",
                [{"type": _SECRET, "value": _SECRET}],
                timestamp=_SECRET,
            ),
        ]
    )
    _write(path, records)

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))
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
        b'{"type":"assistant"',
        b"\xff\xfe",
        b"[]",
    ],
)
async def test_qwen_scanner_failures_poison_parse_confidence(
    tmp_path: Path,
    record: bytes,
) -> None:
    path = tmp_path / "scanner-failure.jsonl"
    _write(path, [record])

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 1
    assert snapshot.has_conclusive_turn_completed is False
    assert snapshot.has_conclusive_capacity_error is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        {"type": "assistant"},
        {"type": "assistant", "message": {"parts": "bad"}},
        {"type": "tool_result", "message": []},
        {"type": "system", "subtype": 7},
        {"type": 7},
    ],
)
async def test_qwen_malformed_recognized_records_poison_parse_confidence(
    tmp_path: Path,
    record: dict[str, object],
) -> None:
    path = tmp_path / "malformed.jsonl"
    _write(path, [record])

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 1


@pytest.mark.asyncio
async def test_qwen_unknown_record_and_blank_line_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "unknown.jsonl"
    _write(
        path,
        [
            _record("assistant", [{"text": "reply"}]),
            "   ",
            {"type": "future_record", "payload": {"value": "ignored"}},
        ],
    )

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num is None
    assert len(snapshot.tail) == 1


@pytest.mark.asyncio
async def test_qwen_empty_transcript_returns_empty_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")

    snapshot = await QwenTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "qwen"
    assert snapshot.tail == ()
    assert snapshot.latest_activity_kind is None
    assert json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_qwen_reader_raises_oserror_for_missing_path(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await QwenTranscriptWatchdogReader().read(str(tmp_path / "missing.jsonl"))
