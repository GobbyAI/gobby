import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.agents.watchdog.grok import GrokTranscriptWatchdogReader

pytestmark = pytest.mark.unit

_TIMESTAMP = 1_753_185_600
_META_TIMESTAMP_MS = 1_753_185_600_125
_SECRET = "grok-reader-secret-that-must-not-survive"
_MISSING = object()
_REAL_FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "acp_contract"
    / "grok-0.1.216-session-load-tool-prompt.stdout.jsonl"
)


def _update(
    update_type: object,
    *,
    timestamp: object = _TIMESTAMP,
    meta_timestamp_ms: object = _MISSING,
    method: object = "session/update",
    **update_fields: object,
) -> dict[str, object]:
    params: dict[str, object] = {
        "sessionId": "session-id",
        "update": {"sessionUpdate": update_type, **update_fields},
    }
    if meta_timestamp_ms is not _MISSING:
        params["_meta"] = {"agentTimestampMs": meta_timestamp_ms}
    record: dict[str, object] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    }
    if timestamp is not _MISSING:
        record["timestamp"] = timestamp
    return record


def _write(path: Path, records: list[dict[str, object] | str]) -> None:
    lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def test_real_grok_fixture_is_well_formed_and_extracts_tool_activity() -> None:
    snapshot = await GrokTranscriptWatchdogReader().read(str(_REAL_FIXTURE))

    assert snapshot.provider == "grok"
    assert snapshot.last_malformed_line_num is None
    assert snapshot.latest_activity_kind == "tool"
    assert snapshot.latest_model_output_line_num == 8
    assert snapshot.has_conclusive_turn_completed is False
    assert snapshot.has_conclusive_capacity_error is False
    json.dumps(snapshot.to_log_dict())


async def test_turn_completed_uses_top_level_epoch_seconds(tmp_path: Path) -> None:
    path = tmp_path / "completed.jsonl"
    _write(
        path,
        [
            _update("user_message_chunk", content={"text": "continue"}),
            _update(
                "turn_completed",
                method="_x.ai/session/update",
                prompt_id="prompt-id",
                stop_reason="end_turn",
            ),
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_kind == "completed"
    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.timestamp == datetime.fromtimestamp(_TIMESTAMP, UTC)
    assert snapshot.has_conclusive_turn_completed is True


async def test_timestamp_falls_back_to_meta_epoch_milliseconds(tmp_path: Path) -> None:
    path = tmp_path / "meta-timestamp.jsonl"
    _write(
        path,
        [
            _update(
                "turn_completed",
                timestamp=_MISSING,
                meta_timestamp_ms=_META_TIMESTAMP_MS,
                method="_x.ai/session/update",
                prompt_id="prompt-id",
                stop_reason="end_turn",
            )
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.timestamp == datetime.fromtimestamp(
        _META_TIMESTAMP_MS / 1000,
        UTC,
    )
    assert snapshot.has_conclusive_turn_completed is True


async def test_new_user_chunk_supersedes_obsolete_completion(tmp_path: Path) -> None:
    path = tmp_path / "new-turn.jsonl"
    _write(
        path,
        [
            _update(
                "turn_completed",
                method="_x.ai/session/update",
                prompt_id="old-prompt",
                stop_reason="end_turn",
            ),
            _update("user_message_chunk", content={"text": "new turn"}),
            _update("agent_thought_chunk", content={"text": "thinking"}),
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_kind == "started"
    assert snapshot.turn_started_event is not None
    assert snapshot.turn_started_event.line_num == 2
    assert snapshot.has_conclusive_turn_completed is False


async def test_retry_state_is_redacted_diagnostic_only(tmp_path: Path) -> None:
    path = tmp_path / "retry.jsonl"
    _write(
        path,
        [
            _update("user_message_chunk", content={"text": _SECRET}),
            _update(
                "retry_state",
                method="_x.ai/session/update",
                type="retrying",
                attempt=2,
                max_retries=15,
                reason=_SECRET,
            ),
            _update(
                "turn_completed",
                method="_x.ai/session/update",
                prompt_id="prompt-id",
                stop_reason="end_turn",
            ),
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))
    encoded = json.dumps(snapshot.to_log_dict())

    assert snapshot.provider_error_kind == "retry"
    assert snapshot.provider_error_reason == "retrying"
    assert snapshot.provider_error_event is not None
    assert snapshot.provider_error_event.line_num == 2
    assert snapshot.has_conclusive_capacity_error is False
    assert snapshot.has_conclusive_turn_completed is True
    assert _SECRET not in encoded


@pytest.mark.parametrize(
    ("update_type", "expected_activity", "expected_model_output_line"),
    [
        ("agent_message_chunk", "message", 1),
        ("agent_thought_chunk", "reasoning", 1),
        ("tool_call", "tool", 1),
        ("tool_call_update", "tool", 1),
        ("tool_call_delta_chunk", "tool", 1),
        ("user_message_chunk", "user_input", None),
    ],
)
async def test_activity_kind(
    tmp_path: Path,
    update_type: str,
    expected_activity: str,
    expected_model_output_line: int | None,
) -> None:
    path = tmp_path / f"{update_type}.jsonl"
    _write(path, [_update(update_type, content={"text": _SECRET})])

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_activity_kind == expected_activity
    assert snapshot.latest_model_output_line_num == expected_model_output_line


async def test_tail_is_bounded_and_structurally_redacted(tmp_path: Path) -> None:
    path = tmp_path / "redacted.jsonl"
    _write(
        path,
        [
            _update(
                "agent_message_chunk",
                timestamp=_SECRET,
                content={"text": _SECRET},
                extra=_SECRET,
            )
            for _ in range(9)
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))
    encoded = json.dumps(snapshot.to_log_dict())

    assert len(snapshot.tail) == 8
    assert all(item.event_type == "session_update" for item in snapshot.tail)
    assert all(item.payload_type == "agent_message_chunk" for item in snapshot.tail)
    assert all(item.timestamp is None for item in snapshot.tail)
    assert _SECRET not in encoded


@pytest.mark.parametrize(
    "bad_record",
    [b'{"method":"session/update"', b"\xff\xfe\n", b"[]\n"],
)
async def test_scanner_failures_poison_conclusiveness(
    tmp_path: Path,
    bad_record: bytes,
) -> None:
    path = tmp_path / "scanner-failure.jsonl"
    completed = _update(
        "turn_completed",
        method="_x.ai/session/update",
        prompt_id="prompt-id",
        stop_reason="end_turn",
    )
    path.write_bytes(json.dumps(completed).encode("utf-8") + b"\n" + bad_record)

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 2
    assert snapshot.has_conclusive_turn_completed is False


@pytest.mark.parametrize(
    "bad_record",
    [
        {"method": "session/update", "params": []},
        {"method": "session/update", "params": {"update": []}},
        {"method": "session/update", "params": {"update": {"sessionUpdate": 7}}},
        _update("turn_completed", stop_reason="end_turn"),
        _update(
            "retry_state",
            method="_x.ai/session/update",
            type="retrying",
            attempt=True,
            max_retries=15,
            reason="overloaded",
        ),
    ],
)
async def test_malformed_recognized_records_poison_conclusiveness(
    tmp_path: Path,
    bad_record: dict[str, object],
) -> None:
    path = tmp_path / "malformed-recognized.jsonl"
    _write(path, [bad_record])

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 1
    assert snapshot.has_conclusive_turn_completed is False


async def test_unknown_records_and_blank_line_do_not_poison_completion(tmp_path: Path) -> None:
    path = tmp_path / "unknown.jsonl"
    _write(
        path,
        [
            _update(
                "turn_completed",
                method="_x.ai/session/update",
                prompt_id="prompt-id",
                stop_reason="end_turn",
            ),
            " ",
            {"jsonrpc": "2.0", "id": 1, "result": {"secret": _SECRET}},
            _update("future_update", future_field=_SECRET),
            {"jsonrpc": "2.0", "method": "terminal/create", "params": {}},
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num is None
    assert snapshot.has_conclusive_turn_completed is True
    assert _SECRET not in json.dumps(snapshot.to_log_dict())


async def test_invalid_completion_timestamp_disables_recovery_and_is_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-timestamp.jsonl"
    _write(
        path,
        [
            _update(
                "turn_completed",
                timestamp=_SECRET,
                meta_timestamp_ms=_SECRET,
                method="_x.ai/session/update",
                prompt_id="prompt-id",
                stop_reason="end_turn",
            )
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.timestamp is None
    assert snapshot.has_conclusive_turn_completed is False
    assert _SECRET not in json.dumps(snapshot.to_log_dict())


async def test_earlier_malformed_record_invalidates_later_completed_turn(tmp_path: Path) -> None:
    path = tmp_path / "earlier-malformed.jsonl"
    _write(
        path,
        [
            {"method": "session/update", "params": []},
            _update(
                "turn_completed",
                method="_x.ai/session/update",
                prompt_id="prompt-id",
                stop_reason="end_turn",
            ),
        ],
    )

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_kind == "completed"
    assert snapshot.last_malformed_line_num == 1
    assert snapshot.has_conclusive_turn_completed is False


async def test_empty_transcript_returns_empty_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    snapshot = await GrokTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "grok"
    assert snapshot.tail == ()
    assert snapshot.latest_turn_event is None
    assert snapshot.last_malformed_line_num is None


async def test_reader_raises_oserror_for_missing_path(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await GrokTranscriptWatchdogReader().read(str(tmp_path / "missing.jsonl"))
