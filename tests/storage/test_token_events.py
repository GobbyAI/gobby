from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, cast

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.token_events import TokenEvent, TokenEventStore, _row_value, merge_event_totals

pytestmark = pytest.mark.unit


class _ExplodingRow:
    def __getitem__(self, key: str) -> object:
        raise ValueError(f"unexpected failure for {key}")


class _ListEventsDb:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.sql = ""
        self.params: tuple[object, ...] = ()

    def fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        self.sql = sql
        self.params = params
        return [self.row]


class _TokenMetadataLogRecord(Protocol):
    id: int
    session_id: str
    project_id: str
    message_id: str
    model: str
    raw_metadata_present: bool
    raw_metadata_size: int


def test_row_value_returns_default_for_expected_access_errors() -> None:
    assert _row_value({}, "id", "fallback") == "fallback"
    assert _row_value([], "id", "fallback") == "fallback"


def test_row_value_propagates_unexpected_errors() -> None:
    with pytest.raises(ValueError, match="unexpected failure"):
        _row_value(_ExplodingRow(), "id")


def test_row_to_event_dict_logs_metadata_context(caplog: pytest.LogCaptureFixture) -> None:
    row = {
        "id": 12,
        "session_id": "sess-1",
        "project_id": "proj-1",
        "message_id": "msg-1",
        "source": "claude",
        "origin": "transcript",
        "model": "claude-sonnet-4",
        "model_family": "claude",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_tokens": 3,
        "cache_read_tokens": 4,
        "context_window": 200000,
        "event_at": "2026-04-08T12:00:00Z",
        "created_at": "2026-04-08T12:00:01Z",
        "metadata": "{not-json",
    }

    with caplog.at_level(logging.DEBUG):
        event = TokenEventStore._row_to_event_dict(row)

    assert event["metadata"] is None

    record = next(
        record
        for record in caplog.records
        if record.message == "Failed to parse token event metadata"
    )
    extra = cast(_TokenMetadataLogRecord, record)
    assert extra.id == 12
    assert extra.session_id == "sess-1"
    assert extra.project_id == "proj-1"
    assert extra.message_id == "msg-1"
    assert extra.model == "claude-sonnet-4"
    assert extra.raw_metadata_present is True
    assert extra.raw_metadata_size == len("{not-json")


def test_list_session_events_rejects_non_positive_limit(temp_db: HubDatabase) -> None:
    store = TokenEventStore(db=temp_db)

    with pytest.raises(ValueError, match="limit must be a positive integer"):
        store.list_session_events("sess-1", limit=0)


def test_list_session_events_includes_events_at_since_boundary() -> None:
    boundary = datetime(2026, 4, 8, 12, 0, tzinfo=UTC)
    db = _ListEventsDb(
        {
            "id": 1,
            "session_id": "sess-boundary",
            "message_id": "msg-boundary",
            "event_at": boundary.isoformat(),
        }
    )
    store = TokenEventStore(db=cast(HubDatabase, db))

    events = store.list_session_events("sess-boundary", since=boundary.isoformat())

    assert "event_at >= %s" in db.sql
    assert db.params == ("sess-boundary", boundary, 500)
    assert [event["message_id"] for event in events] == ["msg-boundary"]


def test_merge_event_totals_coerces_invalid_values() -> None:
    totals = merge_event_totals(
        [
            {
                "input_tokens": "12",
                "output_tokens": "oops",
                "cache_creation_tokens": None,
                "cache_read_tokens": True,
            },
            {
                "input_tokens": "",
                "output_tokens": 8,
                "cache_creation_tokens": "5",
                "cache_read_tokens": " 3 ",
            },
        ]
    )

    assert totals == {
        "input_tokens": 12,
        "output_tokens": 8,
        "cache_creation_tokens": 5,
        "cache_read_tokens": 4,
    }


def _event(message_id: str | None, *, session_id: str = "s1") -> TokenEvent:
    return TokenEvent(
        session_id=session_id,
        project_id=None,
        message_id=message_id,
        source="claude",
        origin="transcript",
        model=None,
        input_tokens=1,
        output_tokens=2,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        event_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )


class _RecordBatchDb:
    """Simulates multi-row INSERT ... ON CONFLICT DO NOTHING ... RETURNING.

    Rows whose (session_id, message_id) key is already present — from a prior
    statement or earlier in the same statement — are omitted from the returned
    rows, exactly like the real conflict clause. NULL message_id rows never
    conflict.
    """

    def __init__(self, existing: set[tuple[str, str]] | None = None) -> None:
        self.existing: set[tuple[str, str]] = set(existing or set())
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def fetchall(
        self, sql: str, params: tuple[object, ...] | list[object] = ()
    ) -> list[dict[str, object]]:
        self.statements.append((sql, tuple(params)))
        rows: list[dict[str, object]] = []
        for start in range(0, len(params), 14):
            session_id = params[start]
            message_id = params[start + 2]
            if message_id is None:
                rows.append({"session_id": session_id, "message_id": None})
                continue
            key = (str(session_id), str(message_id))
            if key in self.existing:
                continue
            self.existing.add(key)
            rows.append({"session_id": session_id, "message_id": message_id})
        return rows


def test_record_batch_issues_statements_per_chunk_not_per_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.storage.token_events.RECORD_BATCH_CHUNK_SIZE", 4)
    db = _RecordBatchDb()
    store = TokenEventStore(db=cast(HubDatabase, db))

    flags = store.record_batch([_event(f"msg-{i}") for i in range(10)])

    assert flags == [True] * 10
    assert len(db.statements) == 3
    row_counts = [len(params) // 14 for _sql, params in db.statements]
    assert row_counts == [4, 4, 2]


def test_record_batch_reports_per_event_dedup_flags() -> None:
    db = _RecordBatchDb(existing={("s1", "msg-live")})
    store = TokenEventStore(db=cast(HubDatabase, db))

    flags = store.record_batch(
        [
            _event("msg-a"),
            _event("msg-a"),  # intra-batch duplicate: first occurrence wins
            _event("msg-live"),  # already recorded (e.g. live-origin row)
            _event(None),  # no message_id: never conflicts
        ]
    )

    assert flags == [True, False, False, True]
    assert len(db.statements) == 1


def test_record_batch_with_no_events_issues_no_statements() -> None:
    db = _RecordBatchDb()
    store = TokenEventStore(db=cast(HubDatabase, db))

    assert store.record_batch([]) == []
    assert db.statements == []
