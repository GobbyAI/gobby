from __future__ import annotations

import logging
from typing import Protocol, cast

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.token_events import TokenEventStore, _row_value, merge_event_totals

pytestmark = pytest.mark.unit


class _ExplodingRow:
    def __getitem__(self, key: str) -> object:
        raise ValueError(f"unexpected failure for {key}")


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


def test_list_session_events_rejects_non_positive_limit(temp_db: LocalDatabase) -> None:
    store = TokenEventStore(db=temp_db)

    with pytest.raises(ValueError, match="limit must be a positive integer"):
        store.list_session_events("sess-1", limit=0)


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
