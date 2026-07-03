from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from gobby.utils.datetime import (
    datetime_to_iso,
    parse_stored_datetime,
    to_aware_utc,
    to_json_safe,
    utc_now,
)


def test_utc_now_returns_aware_utc_datetime() -> None:
    now = utc_now()

    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)


def test_to_aware_utc_treats_naive_values_as_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)

    assert to_aware_utc(naive) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_to_aware_utc_converts_offset_values_to_utc() -> None:
    offset = timezone(-timedelta(hours=5))
    value = datetime(2026, 1, 1, 22, 30, tzinfo=offset)

    assert to_aware_utc(value) == datetime(2026, 1, 2, 3, 30, tzinfo=UTC)


def test_parse_stored_datetime_accepts_iso_strings_and_datetime_objects() -> None:
    assert parse_stored_datetime("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    offset = timezone(timedelta(hours=2))
    assert parse_stored_datetime(datetime(2026, 1, 2, 5, 4, 5, tzinfo=offset)) == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )


def test_datetime_to_iso_serializes_boundary_values_as_utc() -> None:
    offset = timezone(-timedelta(hours=5))
    value = datetime(2026, 1, 1, 22, 30, tzinfo=offset)

    assert datetime_to_iso(value) == "2026-01-02T03:30:00+00:00"
    assert datetime_to_iso(None) is None


def test_to_json_safe_recursively_serializes_datetime_and_date_values() -> None:
    offset = timezone(-timedelta(hours=5))
    value = {
        "when": datetime(2026, 1, 1, 22, 30, tzinfo=offset),
        "dates": (date(2026, 1, 3),),
        "items": [{"created_at": datetime(2026, 1, 2, 3, 30, tzinfo=UTC)}],
    }

    assert to_json_safe(value) == {
        "when": "2026-01-02T03:30:00+00:00",
        "dates": ["2026-01-03"],
        "items": [{"created_at": "2026-01-02T03:30:00+00:00"}],
    }
