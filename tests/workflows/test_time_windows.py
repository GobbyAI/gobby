"""Clock windows declared in rule YAML."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.time_windows import time_in_windows

pytestmark = pytest.mark.unit

CHICAGO = ZoneInfo("America/Chicago")


def _chicago(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CHICAGO)


DAILY = [{"tz": "America/Chicago", "start": "04:00", "end": "05:00"}]
MONDAY = [{"tz": "America/Chicago", "start": "04:00", "end": "06:00", "days": ["mon"]}]
OVERNIGHT_FRIDAY = [{"tz": "UTC", "start": "23:00", "end": "01:00", "days": ["fri"]}]


@pytest.mark.parametrize(
    ("windows", "now", "expected"),
    [
        pytest.param(DAILY, _chicago(2026, 11, 4, 4, 0), True, id="start-inclusive"),
        pytest.param(DAILY, _chicago(2026, 11, 4, 4, 59), True, id="inside"),
        pytest.param(DAILY, _chicago(2026, 11, 4, 5, 0), False, id="end-exclusive"),
        pytest.param(DAILY, _chicago(2026, 11, 4, 3, 59), False, id="before"),
        pytest.param(MONDAY, _chicago(2026, 11, 2, 5, 30), True, id="listed-day"),
        pytest.param(MONDAY, _chicago(2026, 11, 3, 5, 30), False, id="unlisted-day"),
        pytest.param(
            DAILY, datetime(2026, 11, 4, 10, 30, tzinfo=UTC), True, id="utc-converted-inside"
        ),
        pytest.param(
            DAILY, datetime(2026, 11, 4, 4, 30, tzinfo=UTC), False, id="utc-wall-clock-outside"
        ),
        pytest.param(
            OVERNIGHT_FRIDAY,
            datetime(2026, 11, 6, 23, 30, tzinfo=UTC),
            True,
            id="wrap-before-midnight",
        ),
        pytest.param(
            OVERNIGHT_FRIDAY,
            datetime(2026, 11, 7, 0, 30, tzinfo=UTC),
            True,
            id="wrap-after-midnight-counts-start-day",
        ),
        pytest.param(
            OVERNIGHT_FRIDAY,
            datetime(2026, 11, 8, 0, 30, tzinfo=UTC),
            False,
            id="wrap-after-midnight-other-day",
        ),
        pytest.param(
            OVERNIGHT_FRIDAY,
            datetime(2026, 11, 7, 1, 0, tzinfo=UTC),
            False,
            id="wrap-end-exclusive",
        ),
        pytest.param(DAILY + MONDAY, _chicago(2026, 11, 2, 5, 30), True, id="any-window"),
    ],
)
def test_time_in_windows(windows: list[dict[str, Any]], now: datetime, expected: bool) -> None:
    assert time_in_windows(windows, now=now) is expected


@pytest.mark.parametrize(
    "windows",
    [
        pytest.param([], id="empty"),
        pytest.param("04:00-05:00", id="string"),
        pytest.param(["04:00"], id="spec-not-mapping"),
        pytest.param([{"tz": "Mars/Base", "start": "04:00", "end": "05:00"}], id="unknown-tz"),
        pytest.param([{"start": "04:00", "end": "05:00"}], id="missing-tz"),
        pytest.param([{"tz": "UTC", "start": "4:00", "end": "05:00"}], id="loose-clock"),
        pytest.param([{"tz": "UTC", "start": "24:00", "end": "05:00"}], id="hour-range"),
        pytest.param([{"tz": "UTC", "start": "05:00", "end": "05:00"}], id="empty-span"),
        pytest.param(
            [{"tz": "UTC", "start": "04:00", "end": "05:00", "days": ["monday"]}], id="bad-day"
        ),
        pytest.param([{"tz": "UTC", "start": "04:00", "end": "05:00", "days": []}], id="no-days"),
        pytest.param(
            [{"tz": "UTC", "start": "04:00", "end": "05:00", "every": "day"}], id="unknown-key"
        ),
    ],
)
def test_invalid_window_specs_are_rejected(windows: object) -> None:
    with pytest.raises(ValueError, match="time window"):
        time_in_windows(windows, now=datetime(2026, 11, 4, 4, 30, tzinfo=UTC))


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        time_in_windows(DAILY, now=datetime(2026, 11, 4, 4, 30))


def test_rule_conditions_can_call_time_in_windows() -> None:
    evaluator = SafeExpressionEvaluator({}, build_condition_helpers())

    result = evaluator.evaluate_value(
        "time_in_windows([{'tz': 'UTC', 'start': '00:00', 'end': '23:59'}])"
    )

    assert isinstance(result, bool)
