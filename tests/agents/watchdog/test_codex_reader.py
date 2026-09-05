import json
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.agents.watchdog.codex import (
    CODEX_MODEL_CAPACITY_MESSAGE,
    CodexTranscriptWatchdogReader,
)

pytestmark = pytest.mark.unit

_TIMESTAMP = "2026-07-22T12:00:00+00:00"
_SECRET = "reader-secret-that-must-not-survive"


def _record(
    event_type: str,
    payload_type: str,
    *,
    timestamp: object = _TIMESTAMP,
    **payload_fields: object,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": event_type,
        "payload": {"type": payload_type, **payload_fields},
    }


def _write(path: Path, records: Sequence[dict[str, object] | str]) -> None:
    lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_codex_reader_keeps_only_last_eight_redacted_response_items(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    records = [
        _record("response_item", "custom_tool_call", name=_SECRET, arguments=_SECRET)
        for _ in range(9)
    ]
    _write(path, records)

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))
    encoded = json.dumps(snapshot.to_log_dict())

    assert len(snapshot.tail) == 8
    assert all(item.event_type == "response_item" for item in snapshot.tail)
    assert all(item.payload_type == "custom_tool_call" for item in snapshot.tail)
    assert _SECRET not in encoded


@pytest.mark.asyncio
async def test_codex_reader_tracks_latest_turn_marker(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    _write(
        path,
        [
            _record("event_msg", "task_started"),
            _record("event_msg", "task_complete"),
            _record("event_msg", "task_started"),
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.line_num == 3
    assert snapshot.latest_turn_kind == "started"
    assert snapshot.has_conclusive_turn_completed is False


@pytest.mark.asyncio
async def test_codex_reader_scans_only_appended_records(tmp_path: Path) -> None:
    path = tmp_path / "incremental.jsonl"
    _write(path, [_record("event_msg", "task_started")])
    reader = CodexTranscriptWatchdogReader()
    first = await reader.read(str(path))
    assert first.latest_turn_kind == "started"

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("event_msg", "task_complete")) + "\n")
    with patch("gobby.agents.watchdog.codex.json.loads", wraps=json.loads) as loads:
        second = await reader.read(str(path))

    assert loads.call_count == 1
    assert second.latest_turn_event is not None
    assert second.latest_turn_event.line_num == 2
    assert second.latest_turn_kind == "completed"


@pytest.mark.asyncio
async def test_codex_reader_resets_incremental_state_at_session_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "resumed-session.jsonl"
    terminal_error = {
        "message": "You've hit your usage limit. Account-specific reset details.",
        "codex_error_info": "usage_limit_exceeded",
    }
    _write(
        path,
        [
            _record("event_msg", "task_started"),
            _record("event_msg", "task_complete", error=terminal_error),
        ],
    )
    reader = CodexTranscriptWatchdogReader()
    predecessor = await reader.read(str(path))
    assert predecessor.has_conclusive_terminal_provider_error is True

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "session_meta", "payload": {"id": "resumed"}}) + "\n")
    with patch("gobby.agents.watchdog.codex.json.loads", wraps=json.loads) as loads:
        resumed = await reader.read(str(path))

    assert loads.call_count == 1
    assert resumed.has_conclusive_terminal_provider_error is False
    assert resumed.turn_started_event is None
    assert resumed.latest_turn_event is None
    assert resumed.provider_error_event is None
    assert resumed.last_malformed_line_num is None

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record("event_msg", "task_started")) + "\n")
        handle.write(json.dumps(_record("event_msg", "task_complete", error=terminal_error)) + "\n")

    current = await reader.read(str(path))

    assert current.has_conclusive_terminal_provider_error is True
    assert current.provider_error_reason == "usage_limit_exceeded"


@pytest.mark.asyncio
async def test_codex_reader_resets_incremental_state_after_truncation(tmp_path: Path) -> None:
    path = tmp_path / "truncated.jsonl"
    _write(
        path,
        [
            _record("event_msg", "task_started"),
            _record("event_msg", "task_complete"),
        ],
    )
    reader = CodexTranscriptWatchdogReader()
    assert (await reader.read(str(path))).latest_turn_kind == "completed"

    _write(path, [_record("event_msg", "task_started")])
    snapshot = await reader.read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.line_num == 1
    assert snapshot.latest_turn_kind == "started"
    assert snapshot.has_conclusive_turn_completed is False


@pytest.mark.asyncio
async def test_codex_reader_resets_incremental_state_after_file_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replaced.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    _write(path, [_record("event_msg", "task_complete")])
    reader = CodexTranscriptWatchdogReader()
    assert (await reader.read(str(path))).latest_turn_kind == "completed"

    _write(replacement, [_record("event_msg", "task_started")])
    replacement.replace(path)
    snapshot = await reader.read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.line_num == 1
    assert snapshot.latest_turn_kind == "started"


@pytest.mark.asyncio
async def test_codex_reader_confirms_capacity_with_blank_lines_interleaved(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capacity.jsonl"
    records: list[dict[str, object] | str] = [
        _record("event_msg", "task_started"),
        "   ",
        _record(
            "event_msg",
            "error",
            message=CODEX_MODEL_CAPACITY_MESSAGE,
            codex_error_info="server_overloaded",
        ),
        "",
        _record("event_msg", "task_complete"),
    ]
    _write(path, records)

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.has_conclusive_capacity_error is True
    assert snapshot.provider_error_event is not None
    assert snapshot.provider_error_event.line_num == 3
    assert snapshot.provider_error_reason == "server_overloaded"
    assert CODEX_MODEL_CAPACITY_MESSAGE not in json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_codex_reader_classifies_task_complete_usage_limit_as_terminal_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "usage-limit.jsonl"
    private_message = "You've hit your usage limit. Account-specific reset details."
    _write(
        path,
        [
            _record("event_msg", "task_started"),
            _record(
                "event_msg",
                "task_complete",
                error={
                    "message": private_message,
                    "codex_error_info": "usage_limit_exceeded",
                },
            ),
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.has_conclusive_terminal_provider_error is True
    assert snapshot.provider_error_event == snapshot.latest_turn_event
    assert snapshot.provider_error_kind == "terminal"
    assert snapshot.provider_error_reason == "usage_limit_exceeded"
    assert private_message not in json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_codex_reader_keeps_server_overload_task_complete_recoverable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "server-overloaded.jsonl"
    _write(
        path,
        [
            _record("event_msg", "task_started"),
            _record(
                "event_msg",
                "task_complete",
                error={
                    "message": CODEX_MODEL_CAPACITY_MESSAGE,
                    "codex_error_info": "server_overloaded",
                },
            ),
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.has_conclusive_terminal_provider_error is False
    assert snapshot.has_conclusive_turn_completed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param({}, id="empty"),
        pytest.param(
            {"message": "", "codex_error_info": None},
            id="empty-fields",
        ),
    ],
)
async def test_codex_reader_ignores_empty_task_complete_error(
    tmp_path: Path,
    error: dict[str, object],
) -> None:
    path = tmp_path / "empty-error.jsonl"
    _write(
        path,
        [
            _record("event_msg", "task_started"),
            _record("event_msg", "task_complete", error=error),
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.has_conclusive_terminal_provider_error is False
    assert snapshot.has_conclusive_turn_completed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "error_info", "suffix", "malformed_tail"),
    [
        ("different error", "server_overloaded", [], False),
        (CODEX_MODEL_CAPACITY_MESSAGE, "different_info", [], False),
        (CODEX_MODEL_CAPACITY_MESSAGE, "server_overloaded", ["task_started"], False),
        (CODEX_MODEL_CAPACITY_MESSAGE, "server_overloaded", [], True),
    ],
    ids=["wrong-message", "wrong-info", "later-start", "malformed"],
)
async def test_codex_reader_rejects_inconclusive_capacity_errors(
    tmp_path: Path,
    message: str,
    error_info: str,
    suffix: list[str],
    malformed_tail: bool,
) -> None:
    path = tmp_path / "inconclusive.jsonl"
    records: list[dict[str, object] | str] = [
        _record("event_msg", "task_started"),
        _record("event_msg", "error", message=message, codex_error_info=error_info),
        _record("event_msg", "task_complete"),
        *[_record("event_msg", marker) for marker in suffix],
    ]
    if malformed_tail:
        records.append("{broken")
    _write(path, records)

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.has_conclusive_capacity_error is False


@pytest.mark.asyncio
async def test_agent_message_after_reasoning_preserves_interrupt_activity_parity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reasoning.jsonl"
    _write(
        path,
        [
            _record("response_item", "reasoning", summary=[_SECRET]),
            _record("event_msg", "agent_message", message=_SECRET),
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_activity_kind == "reasoning"
    assert snapshot.latest_model_output_line_num == 2
    assert [item.payload_type for item in snapshot.tail] == ["reasoning"]


@pytest.mark.asyncio
async def test_task_complete_followed_by_turn_aborted_suppresses_completion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "aborted.jsonl"
    _write(
        path,
        [
            _record("event_msg", "task_started"),
            _record("event_msg", "task_complete"),
            _record("event_msg", "turn_aborted", reason=_SECRET),
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_kind == "aborted"
    assert snapshot.has_conclusive_turn_completed is False


@pytest.mark.asyncio
async def test_context_compacted_is_inert(tmp_path: Path) -> None:
    path = tmp_path / "compacted.jsonl"
    _write(
        path,
        [
            _record("event_msg", "task_complete"),
            _record("event_msg", "context_compacted", content=_SECRET),
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_kind == "completed"
    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.line_num == 1
    assert snapshot.tail == ()


@pytest.mark.asyncio
async def test_user_only_retry_does_not_count_as_model_output(tmp_path: Path) -> None:
    path = tmp_path / "user-only.jsonl"
    _write(path, [_record("response_item", "message", role="user", content=_SECRET)])

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_activity_kind == "user_input"
    assert snapshot.latest_model_output_line_num is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_record",
    [b"{broken\n", b"[]\n", b'{"type":"event_msg","payload":[]}\n', b"\xff\n"],
    ids=["json", "nondict", "recognized-shape", "utf8"],
)
async def test_malformed_records_poison_conclusiveness(
    tmp_path: Path,
    bad_record: bytes,
) -> None:
    path = tmp_path / "malformed.jsonl"
    prefix = json.dumps(_record("event_msg", "task_complete")).encode("utf-8") + b"\n"
    path.write_bytes(prefix + bad_record)

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num == 2
    assert snapshot.has_conclusive_turn_completed is False


@pytest.mark.asyncio
async def test_unknown_record_and_blank_line_do_not_poison_completion(tmp_path: Path) -> None:
    path = tmp_path / "unknown.jsonl"
    _write(
        path,
        [
            _record("event_msg", "task_complete"),
            " ",
            {"type": "future_record", "secret": _SECRET},
        ],
    )

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.last_malformed_line_num is None
    assert snapshot.has_conclusive_turn_completed is True
    assert _SECRET not in json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_invalid_completion_timestamp_disables_recovery_and_is_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-timestamp.jsonl"
    _write(path, [_record("event_msg", "task_complete", timestamp=_SECRET)])

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.latest_turn_event is not None
    assert snapshot.latest_turn_event.timestamp is None
    assert snapshot.has_conclusive_turn_completed is False
    assert _SECRET not in json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_reader_raises_oserror_for_missing_path(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await CodexTranscriptWatchdogReader().read(str(tmp_path / "missing.jsonl"))


@pytest.mark.asyncio
async def test_empty_transcript_returns_empty_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))

    assert snapshot.provider == "codex"
    assert snapshot.tail == ()
    assert snapshot.latest_turn_event is None
    assert snapshot.last_malformed_line_num is None
    assert json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_secret_response_item_labels_are_coerced_and_redacted(tmp_path: Path) -> None:
    path = tmp_path / "secret-labels.jsonl"
    _write(path, [_record("response_item", _SECRET, timestamp=_SECRET)])

    snapshot = await CodexTranscriptWatchdogReader().read(str(path))
    encoded = json.dumps(snapshot.to_log_dict())

    assert snapshot.last_malformed_line_num is None
    assert len(snapshot.tail) == 1
    assert snapshot.tail[0].payload_type == "other"
    assert snapshot.tail[0].timestamp is None
    assert _SECRET not in encoded
