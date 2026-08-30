import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest

from gobby.agents.watchdog.models import (
    KNOWN_WATCHDOG_PROVIDERS,
    WATCHDOG_TAIL_LIMIT,
    TranscriptEventSummary,
    WatchdogTranscriptSnapshot,
)

pytestmark = pytest.mark.unit

_SECRET = "direct-constructor-secret"


def _event(line_num: int = 1) -> TranscriptEventSummary:
    return TranscriptEventSummary(
        line_num=line_num,
        timestamp=datetime(2026, 7, 22, 12, tzinfo=UTC),
        event_type="event_msg",
        payload_type="task_complete",
    )


def test_agy_is_a_known_watchdog_provider() -> None:
    snapshot = WatchdogTranscriptSnapshot(provider="agy")

    assert "agy" in KNOWN_WATCHDOG_PROVIDERS
    assert snapshot.provider == "agy"


def test_event_summary_coerces_content_bearing_fields_and_serializes_json() -> None:
    summary = TranscriptEventSummary(
        line_num=1,
        timestamp=cast(Any, _SECRET),
        event_type=_SECRET,
        payload_type=_SECRET,
    )

    encoded = json.dumps(summary.to_log_dict())

    assert summary.timestamp is None
    assert summary.event_type == "other"
    assert summary.payload_type == "other"
    assert _SECRET not in encoded


def test_event_summary_normalizes_naive_and_aware_timestamps_to_utc() -> None:
    naive = TranscriptEventSummary(1, datetime(2026, 7, 22, 12), "event_msg", "task_complete")
    offset = TranscriptEventSummary(
        2,
        datetime(2026, 7, 22, 12, tzinfo=timezone(timedelta(hours=2))),
        "event_msg",
        "task_complete",
    )

    assert naive.timestamp == datetime(2026, 7, 22, 12, tzinfo=UTC)
    assert offset.timestamp == datetime(2026, 7, 22, 10, tzinfo=UTC)
    assert json.dumps(naive.to_log_dict())


@pytest.mark.parametrize("field", ["event_type", "payload_type"])
def test_event_summary_coerces_non_string_labels(field: str) -> None:
    kwargs: dict[str, Any] = {
        "line_num": 1,
        "timestamp": None,
        "event_type": "event_msg",
        "payload_type": "task_complete",
    }
    kwargs[field] = [_SECRET]

    summary = TranscriptEventSummary(**kwargs)

    assert getattr(summary, field) == "other"
    assert _SECRET not in json.dumps(summary.to_log_dict())


@pytest.mark.parametrize("line_num", [_SECRET, True, 1.5])
def test_event_summary_rejects_invalid_line_numbers(line_num: object) -> None:
    with pytest.raises(TypeError, match="line_num"):
        TranscriptEventSummary(
            line_num=cast(Any, line_num),
            timestamp=None,
            event_type="event_msg",
            payload_type="task_complete",
        )


def test_snapshot_coerces_every_categorical_field_and_truncates_tail() -> None:
    overflow = 3
    tail = tuple(_event(line_num) for line_num in range(1, WATCHDOG_TAIL_LIMIT + overflow))
    snapshot = WatchdogTranscriptSnapshot(
        provider=_SECRET,
        tail=tail,
        turn_started_event=tail[0],
        latest_turn_event=tail[-1],
        latest_turn_kind=cast(Any, _SECRET),
        provider_error_event=tail[1],
        provider_error_kind=cast(Any, _SECRET),
        provider_error_reason=_SECRET,
        latest_activity_kind=cast(Any, _SECRET),
        latest_model_output_line_num=9,
        last_malformed_line_num=10,
    )

    encoded = json.dumps(snapshot.to_log_dict())

    assert snapshot.provider == "unknown"
    assert [item.line_num for item in snapshot.tail] == list(
        range(overflow, WATCHDOG_TAIL_LIMIT + overflow)
    )
    assert snapshot.latest_turn_kind is None
    assert snapshot.provider_error_kind is None
    assert snapshot.provider_error_reason is None
    assert snapshot.latest_activity_kind is None
    assert _SECRET not in encoded


@pytest.mark.parametrize(
    "field",
    [
        "latest_turn_kind",
        "provider_error_kind",
        "provider_error_reason",
        "latest_activity_kind",
    ],
)
def test_snapshot_coerces_non_string_categories(field: str) -> None:
    kwargs: dict[str, Any] = {"provider": "codex", field: [_SECRET]}

    snapshot = WatchdogTranscriptSnapshot(**kwargs)

    assert getattr(snapshot, field) is None
    assert _SECRET not in json.dumps(snapshot.to_log_dict())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tail", [_event()]),
        ("tail", (_SECRET,)),
        ("turn_started_event", _SECRET),
        ("latest_turn_event", _SECRET),
        ("provider_error_event", _SECRET),
        ("latest_model_output_line_num", _SECRET),
        ("latest_model_output_line_num", True),
        ("last_malformed_line_num", _SECRET),
        ("last_malformed_line_num", False),
    ],
)
def test_snapshot_rejects_invalid_structural_fields(field: str, value: object) -> None:
    kwargs: dict[str, Any] = {"provider": "codex", field: value}

    with pytest.raises(TypeError):
        WatchdogTranscriptSnapshot(**kwargs)


def test_snapshot_conclusive_properties_require_valid_order_and_timestamp() -> None:
    started = TranscriptEventSummary(1, None, "event_msg", "task_started")
    error = TranscriptEventSummary(2, None, "event_msg", "error")
    completed = TranscriptEventSummary(
        3,
        datetime(2026, 7, 22, 12, tzinfo=UTC),
        "event_msg",
        "task_complete",
    )
    snapshot = WatchdogTranscriptSnapshot(
        provider="codex",
        turn_started_event=started,
        latest_turn_event=completed,
        latest_turn_kind="completed",
        provider_error_event=error,
        provider_error_kind="capacity",
        provider_error_reason="server_overloaded",
    )

    assert snapshot.has_conclusive_turn_completed is True
    assert snapshot.has_conclusive_capacity_error is True
    assert json.dumps(snapshot.to_log_dict())

    active_after_completion = WatchdogTranscriptSnapshot(
        provider="claude",
        latest_turn_event=completed,
        latest_turn_kind="completed",
        latest_model_output_line_num=4,
    )
    assert active_after_completion.has_conclusive_turn_completed is False
