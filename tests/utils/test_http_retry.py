"""Tests for shared HTTP retry helpers."""

from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from gobby.utils.http_retry import parse_retry_after

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("2.5", 2.5),
        ("-1", 0.0),
        ("999", 5.0),
        ("invalid", None),
    ],
)
def test_parse_retry_after_numeric_and_invalid_values(
    value: str | None,
    expected: float | None,
) -> None:
    assert parse_retry_after(value, max_delay=5.0) == expected


def test_parse_retry_after_http_date_uses_supplied_clock() -> None:
    now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    retry_at = datetime(2026, 7, 31, 12, 0, 3, tzinfo=UTC)

    result = parse_retry_after(
        format_datetime(retry_at, usegmt=True),
        max_delay=5.0,
        now=now,
    )

    assert result == 3.0
