from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from gobby.utils.datetime import (
    datetime_to_iso,
    datetime_to_local_iso,
    datetime_to_required_iso,
    is_valid_timezone,
    normalize_datetime_model,
    parse_stored_datetime,
    require_stored_datetime,
    resolve_local_timezone,
    to_aware_utc,
    to_json_safe,
    to_json_safe_dict,
    utc_now,
)

pytestmark = pytest.mark.unit


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


def test_parse_stored_datetime_accepts_none() -> None:
    assert parse_stored_datetime(None) is None


def test_parse_stored_datetime_treats_naive_values_as_utc() -> None:
    assert parse_stored_datetime("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_parse_stored_datetime_normalizes_aware_values_to_utc() -> None:
    assert parse_stored_datetime(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )

    offset = timezone(timedelta(hours=2))
    assert parse_stored_datetime("2026-01-02T05:04:05+02:00") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    assert parse_stored_datetime(datetime(2026, 1, 2, 5, 4, 5, tzinfo=offset)) == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )


def test_parse_stored_datetime_ignores_non_string_non_datetime_values() -> None:
    assert parse_stored_datetime(object()) is None


def test_parse_stored_datetime_rejects_malformed_strings() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        parse_stored_datetime("not-a-timestamp")


def test_require_stored_datetime_rejects_missing_values() -> None:
    with pytest.raises(ValueError, match="created_at is required"):
        require_stored_datetime(None, "created_at")


def test_datetime_to_iso_serializes_boundary_values_as_utc() -> None:
    offset = timezone(-timedelta(hours=5))
    value = datetime(2026, 1, 1, 22, 30, tzinfo=offset)

    assert datetime_to_iso(value) == "2026-01-02T03:30:00+00:00"
    assert datetime_to_iso(None) is None


def test_datetime_to_required_iso_serializes_non_optional_values() -> None:
    offset = timezone(-timedelta(hours=5))
    value = datetime(2026, 1, 1, 22, 30, tzinfo=offset)

    assert datetime_to_required_iso(value) == "2026-01-02T03:30:00+00:00"


@pytest.fixture
def host_timezone() -> Iterator[Callable[[str], None]]:
    """Repoint the process timezone, restoring the original on teardown."""
    original = os.environ.get("TZ")

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield _set

    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_datetime_to_local_iso_renders_the_stored_instant_in_host_time(
    host_timezone: Callable[[str], None],
) -> None:
    host_timezone("America/Chicago")
    stored = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)

    assert datetime_to_local_iso(stored) == "2026-07-29T21:00:00-05:00"
    assert datetime_to_local_iso(None) is None


def test_resolve_local_timezone_prefers_explicit_then_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "America/Chicago")

    assert resolve_local_timezone("Europe/Berlin") == "Europe/Berlin"
    assert resolve_local_timezone("Not/AZone") == "America/Chicago"
    assert resolve_local_timezone("  ") == "America/Chicago"
    assert resolve_local_timezone() == "America/Chicago"


def test_resolve_local_timezone_ignores_unusable_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``:``-prefixed or unknown TZ falls through to the host zone."""
    monkeypatch.setenv("TZ", ":/etc/localtime")
    from_colon_form = resolve_local_timezone()
    monkeypatch.setenv("TZ", "Not/AZone")
    from_unknown_zone = resolve_local_timezone()

    assert from_colon_form != ":/etc/localtime"
    assert is_valid_timezone(from_colon_form)
    assert from_unknown_zone == from_colon_form


def test_is_valid_timezone_rejects_unknown_names() -> None:
    assert is_valid_timezone("America/Chicago")
    assert not is_valid_timezone("Not/AZone")


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


def test_to_json_safe_dict_preserves_mapping_return_type() -> None:
    value = {"created_at": datetime(2026, 1, 2, 3, 30, tzinfo=UTC)}

    result = to_json_safe_dict(value)

    assert result == {"created_at": "2026-01-02T03:30:00+00:00"}


def test_normalize_datetime_model_keeps_fields_datetime_and_serializes_dicts() -> None:
    @normalize_datetime_model(required=("created_at",), optional=("updated_at",))
    @dataclass
    class RowModel:
        created_at: datetime
        updated_at: datetime | None

        def to_dict(self) -> dict[str, object]:
            return {"created_at": self.created_at, "updated_at": self.updated_at}

    model = RowModel(
        created_at="2026-01-02T03:04:05",
        updated_at=datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2))),
    )

    assert model.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert model.updated_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert model.to_dict() == {
        "created_at": "2026-01-02T03:04:05+00:00",
        "updated_at": "2026-01-02T03:04:05+00:00",
    }
