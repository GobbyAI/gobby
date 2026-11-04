"""Clock windows for rule conditions.

A window is declared in YAML as ``{tz, start, end, days?}``. ``start`` and ``end`` are
``HH:MM`` wall-clock times in ``tz``; the end is exclusive. A start later than the end
wraps past midnight, and the hours after midnight belong to the day the window started.
``days`` lists ``mon`` through ``sun``; omitted means every day.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_KEYS = frozenset({"tz", "start", "end", "days"})


@dataclass(frozen=True)
class TimeWindow:
    tz: ZoneInfo
    start: time
    end: time
    days: frozenset[int] | None

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(self.tz)
        clock = local.time()
        if self.start < self.end:
            inside, day = self.start <= clock < self.end, local.weekday()
        elif clock >= self.start:
            inside, day = True, local.weekday()
        else:
            inside, day = clock < self.end, (local - timedelta(days=1)).weekday()
        return inside and (self.days is None or day in self.days)


def _clock(spec: Mapping[object, object], key: str) -> time:
    value = spec.get(key)
    match = _CLOCK.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"time window {key} must be HH:MM, got {value!r}")
    return time(int(match[1]), int(match[2]))


def _zone(spec: Mapping[object, object]) -> ZoneInfo:
    name = spec.get("tz")
    if not isinstance(name, str) or not name:
        raise ValueError(f"time window tz must be an IANA zone name, got {name!r}")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"time window tz {name!r} is not a known zone") from exc


def _days(spec: Mapping[object, object]) -> frozenset[int] | None:
    if "days" not in spec:
        return None
    days = spec["days"]
    if (
        isinstance(days, str)
        or not isinstance(days, Sequence)
        or not days
        or any(day not in _DAYS for day in days)
    ):
        raise ValueError(f"time window days must be a nonempty list of {_DAYS}, got {days!r}")
    return frozenset(_DAYS.index(day) for day in days)


def parse_window(spec: object) -> TimeWindow:
    """Validate one ``{tz, start, end, days?}`` spec."""
    if not isinstance(spec, Mapping):
        raise ValueError(f"time window must be a mapping, got {spec!r}")
    unknown = set(spec) - _KEYS
    if unknown:
        raise ValueError(f"time window has unknown keys {sorted(map(str, unknown))}")
    start, end = _clock(spec, "start"), _clock(spec, "end")
    if start == end:
        raise ValueError(f"time window start and end are both {start:%H:%M}")
    return TimeWindow(tz=_zone(spec), start=start, end=end, days=_days(spec))


def time_in_windows(windows: object, *, now: datetime | None = None) -> bool:
    """Whether ``now`` (default: the current time) falls inside any window.

    Every spec is validated before any is evaluated, so an invalid spec always raises.
    """
    if isinstance(windows, str) or not isinstance(windows, Sequence) or not windows:
        raise ValueError(f"time windows must be a nonempty list, got {windows!r}")
    parsed = [parse_window(spec) for spec in windows]
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return any(window.contains(moment) for window in parsed)
