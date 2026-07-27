import json
from pathlib import Path

import pytest

from gobby.agents.watchdog.claude import ClaudeTranscriptWatchdogReader

pytestmark = pytest.mark.unit

_TIMESTAMP = "2026-07-22T12:00:00+00:00"
_SECRET = "claude-reader-secret-that-must-not-survive"


def _user_record(
    content: object = "continue",
    *,
    timestamp: object = _TIMESTAMP,
) -> dict[str, object]:
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": content},
    }


def _assistant_record(
    block_type: object = "text",
    *,
    timestamp: object = _TIMESTAMP,
    api_error: object = False,
    error: object | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [{"type": block_type, "text": "private model output"}],
        },
        "isApiErrorMessage": api_error,
    }
    if error is not None:
        record["error"] = error
    return record


def _turn_duration_record(
    *,
    timestamp: object = _TIMESTAMP,
    duration_ms: object = 1200,
    message_count: object = 3,
) -> dict[str, object]:
    return {
        "type": "system",
        "subtype": "turn_duration",
        "timestamp": timestamp,
        "durationMs": duration_ms,
        "messageCount": message_count,
    }


def _write(path: Path, records: list[dict[str, object] | str]) -> None:
    path.write_text(
        "\n".join(record if isinstance(record, str) else json.dumps(record) for record in records)
        + "\n",
        encoding="utf-8",
    )


async def test_claude_reader_extracts_completed_turn_and_reasoning_activity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "claude.jsonl"
    _write(path, [_user_record(), _assistant_record("thinking"), _turn_duration_record()])

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "claude"
    assert snapshot.turn_started_event is not None
    assert snapshot.turn_started_event.line_num == 1
    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.line_num == 3
    assert snapshot.latest_turn_event.payload_type == "turn_duration"
    assert snapshot.latest_turn_kind == "completed"
    assert snapshot.has_conclusive_turn_completed is True
    assert snapshot.latest_activity_kind == "reasoning"
    assert snapshot.latest_model_output_line_num == 2
    assert len(snapshot.tail) == 3


async def test_user_tool_result_preserves_completed_turn_bookkeeping(tmp_path: Path) -> None:
    path = tmp_path / "next-turn.jsonl"
    _write(
        path,
        [
            _turn_duration_record(),
            _user_record(
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "private tool output",
                    }
                ]
            ),
            _assistant_record("thinking"),
        ],
    )

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.line_num == 1
    assert snapshot.latest_turn_event.event_type == "system"
    assert snapshot.latest_turn_kind == "completed"
    assert snapshot.has_conclusive_turn_completed is False


async def test_api_error_is_redacted_diagnostic_only(tmp_path: Path) -> None:
    path = tmp_path / "api-error.jsonl"
    _write(
        path,
        [
            _user_record(),
            _assistant_record("text", api_error=True, error=_SECRET),
            _turn_duration_record(),
        ],
    )

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))
    encoded = json.dumps(snapshot.to_log_dict())

    assert snapshot.provider_error_event is not None
    assert snapshot.provider_error_event.line_num == 2
    assert snapshot.provider_error_event.payload_type == "api_error"
    assert snapshot.provider_error_kind == "api_error"
    assert snapshot.provider_error_reason == "api_error"
    assert snapshot.has_conclusive_capacity_error is False
    assert snapshot.has_conclusive_turn_completed is True
    assert _SECRET not in encoded


@pytest.mark.parametrize(
    ("block_type", "expected"),
    [
        ("thinking", "reasoning"),
        ("text", "message"),
        ("tool_use", "tool"),
        ("future_block", "other"),
    ],
)
async def test_assistant_activity_kind(
    block_type: str,
    expected: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"{block_type}.jsonl"
    _write(path, [_assistant_record(block_type)])

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_activity_kind == expected
    assert snapshot.latest_model_output_line_num == 1


async def test_tail_is_bounded_and_structurally_redacted(tmp_path: Path) -> None:
    path = tmp_path / "redacted.jsonl"
    records = [{"type": _SECRET, "timestamp": _SECRET, "error": _SECRET}]
    records.extend(
        _assistant_record(
            _SECRET,
            timestamp=_SECRET,
            api_error=True,
            error=_SECRET,
        )
        for _ in range(9)
    )
    _write(path, records)

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))
    encoded = json.dumps(snapshot.to_log_dict())

    assert len(snapshot.tail) == 8
    assert all(item.event_type == "assistant" for item in snapshot.tail)
    assert all(item.payload_type == "api_error" for item in snapshot.tail)
    assert all(item.timestamp is None for item in snapshot.tail)
    assert _SECRET not in encoded


@pytest.mark.parametrize(
    "bad_record",
    [
        b'{"type":"system","subtype":"turn_duration"',
        b"\xff\xfe\n",
        b"[]\n",
    ],
)
async def test_scanner_failures_poison_conclusiveness(
    bad_record: bytes,
    tmp_path: Path,
) -> None:
    path = tmp_path / "scanner-failure.jsonl"
    prefix = json.dumps(_turn_duration_record()).encode("utf-8") + b"\n"
    path.write_bytes(prefix + bad_record)

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 2
    assert snapshot.has_conclusive_turn_completed is False


@pytest.mark.parametrize(
    "malformed",
    [
        {"type": "assistant", "message": "invalid", "timestamp": _TIMESTAMP},
        {"type": "user", "message": [], "timestamp": _TIMESTAMP},
        {"type": "system", "subtype": 42, "timestamp": _TIMESTAMP},
        _turn_duration_record(duration_ms="invalid"),
        _turn_duration_record(message_count=True),
    ],
)
async def test_malformed_recognized_records_poison_conclusiveness(
    malformed: dict[str, object],
    tmp_path: Path,
) -> None:
    path = tmp_path / "recognized-malformed.jsonl"
    _write(path, [_turn_duration_record(), malformed])

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 2
    assert snapshot.has_conclusive_turn_completed is False


async def test_unknown_record_and_blank_line_do_not_poison_completion(tmp_path: Path) -> None:
    path = tmp_path / "unknown.jsonl"
    _write(
        path,
        [_turn_duration_record(), " ", {"type": "future_record", "secret": _SECRET}],
    )

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num is None
    assert snapshot.has_conclusive_turn_completed is True
    assert _SECRET not in json.dumps(snapshot.to_log_dict())


async def test_invalid_completion_timestamp_disables_recovery_and_is_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-timestamp.jsonl"
    _write(path, [_turn_duration_record(timestamp=_SECRET)])

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.timestamp is None
    assert snapshot.has_conclusive_turn_completed is False
    assert _SECRET not in json.dumps(snapshot.to_log_dict())


async def test_earlier_malformed_record_invalidates_later_completed_turn(tmp_path: Path) -> None:
    path = tmp_path / "multi-turn-malformed.jsonl"
    _write(
        path,
        [
            {"type": "assistant", "message": None, "timestamp": _TIMESTAMP},
            _user_record(),
            _assistant_record(),
            _turn_duration_record(),
        ],
    )

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 1
    assert snapshot.latest_turn_kind == "completed"
    assert snapshot.has_conclusive_turn_completed is False


async def test_empty_transcript_returns_empty_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")

    snapshot = await ClaudeTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "claude"
    assert snapshot.tail == ()
    assert snapshot.latest_turn_event is None
    assert snapshot.last_malformed_line_num is None
    assert json.dumps(snapshot.to_log_dict())


async def test_reader_raises_oserror_for_missing_path(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await ClaudeTranscriptWatchdogReader().read(str(tmp_path / "missing.jsonl"))
