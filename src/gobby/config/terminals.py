"""Backend-neutral terminal configuration consumed before the host exists."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

_ISO_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
_CAL_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ITEM = re.compile(r"^- ([A-Za-z0-9_.]+):\s*(.*)$")
_FRESHNESS = timedelta(hours=24)


class TerminalConfig(BaseModel):
    """Shared terminal settings for spawn, reaping, and REST/WS surfaces."""

    default_backend: Literal["tmux", "native"] = Field(
        default="native",
        description="Default TerminalRuntime backend for new Gobby-owned terminals.",
    )
    spawn_in_doubt_seconds: float = Field(
        default=150.0,
        gt=0,
        description=(
            "Age below which a pending spawn is in doubt, not dead. "
            "Defaults to the tmux init timeout (120s) plus a 30s margin."
        ),
    )
    hook_write_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=30.0,
        description="How long a hook thread waits for a coordinator write to dispatch.",
    )
    hook_write_shutdown_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=30.0,
        description="How long shutdown drains in-flight TerminalEffectBridge tasks.",
    )


@dataclass(frozen=True)
class FlipGateResult:
    """Outcome of the native-default flip evidence checker."""

    ok: bool
    reasons: tuple[str, ...]


@dataclass
class _Slot:
    weekly_slot: str
    monday: date | None
    calendar_day: date | None
    platforms: set[str] = field(default_factory=set)
    timestamps: list[datetime] = field(default_factory=list)
    package_install: bool = False
    focused_43: bool = False
    focused_36: bool = False


def iso_week_monday(slot: str) -> date | None:
    """Return the Monday (UTC calendar date) for an ISO year-week, or None."""
    match = _ISO_WEEK.fullmatch(slot.strip())
    if match is None:
        return None
    try:
        return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return None


def check_native_backend_flip(text: str) -> FlipGateResult:
    """Return whether flip evidence records two adjacent weekly slots that pass."""
    reasons: list[str] = []
    slots: dict[str, _Slot] = {}
    bug_count: int | None = None
    bug_query = ""
    bug_ts: datetime | None = None
    for title, body in _iter_sections(text):
        items = _parse_items(body)
        heading = title.lower()
        if heading.startswith("run"):
            _ingest_run(slots, items, reasons)
            continue
        if "bug" in heading or heading.startswith("open"):
            bug_count, bug_query, bug_ts = _ingest_bugs(items, reasons)
    _check_slots(slots, reasons)
    qualifying = [
        slot
        for slot in slots.values()
        if slot.monday is not None
        and "macos" in slot.platforms
        and "linux" in slot.platforms
        and slot.package_install
        and slot.focused_43
        and slot.focused_36
    ]
    pair = _latest_adjacent_pair(qualifying, reasons)
    if pair is None:
        if "not_adjacent" not in reasons:
            reasons.append("not_adjacent")
    else:
        later_ts = max(pair[1].timestamps, default=None)
        _check_bug_freshness(bug_count, bug_query, bug_ts, later_ts, reasons)
    unique: list[str] = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    return FlipGateResult(ok=not unique, reasons=tuple(unique))


def _iter_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    title = ""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if title:
                sections.append((title, "\n".join(lines)))
            title = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    if title:
        sections.append((title, "\n".join(lines)))
    return sections


def _parse_items(block: str) -> dict[str, str]:
    items: dict[str, str] = {}
    for raw in block.splitlines():
        match = _ITEM.match(raw.strip())
        if match is not None:
            items[match.group(1)] = match.group(2).strip().strip("`").strip()
    return items


def _ingest_run(slots: dict[str, _Slot], items: dict[str, str], reasons: list[str]) -> None:
    slot_id = items.get("weekly_slot")
    if not slot_id:
        reasons.append("missing_weekly_slot")
        return
    slot = slots.get(slot_id)
    if slot is None:
        slot = _Slot(
            weekly_slot=slot_id,
            monday=iso_week_monday(slot_id),
            calendar_day=_calendar_day(slot_id),
        )
        slots[slot_id] = slot
        if slot.monday is None:
            reasons.append("invalid_slot")
    kind = _platform_kind(items.get("platform", ""))
    if kind is not None:
        slot.platforms.add(kind)
    timestamp = _parse_utc(items.get("utc_timestamp", ""))
    if timestamp is not None:
        slot.timestamps.append(timestamp)
    if _is_pass(items.get("package_install")):
        slot.package_install = True
    if _is_pass(items.get("4.3")):
        slot.focused_43 = True
    if _is_pass(items.get("3.6")):
        slot.focused_36 = True


def _ingest_bugs(
    items: dict[str, str],
    reasons: list[str],
) -> tuple[int | None, str, datetime | None]:
    raw_count = items.get("count")
    query = items.get("query", "")
    timestamp = _parse_utc(items.get("query_timestamp", ""))
    count: int | None
    if raw_count is None:
        reasons.append("missing_bug_count")
        count = None
    else:
        try:
            count = int(raw_count)
        except ValueError:
            reasons.append("invalid_bug_count")
            count = None
    if not query:
        reasons.append("missing_bug_query")
    if timestamp is None:
        reasons.append("missing_bug_timestamp")
    return count, query, timestamp


def _check_slots(slots: dict[str, _Slot], reasons: list[str]) -> None:
    days = [slot.calendar_day for slot in slots.values() if slot.calendar_day is not None]
    days.sort()
    for left, right in zip(days, days[1:], strict=False):
        if (right - left).days == 1:
            reasons.append("one_day_apart")
            break
    for slot in slots.values():
        if "macos" not in slot.platforms or "linux" not in slot.platforms:
            reasons.append("missing_platform")
        if not slot.package_install:
            reasons.append("missing_package_install")
        if not slot.focused_43:
            reasons.append("missing_4.3")
        if not slot.focused_36:
            reasons.append("missing_3.6")


def _latest_adjacent_pair(
    qualifying: list[_Slot], reasons: list[str]
) -> tuple[_Slot, _Slot] | None:
    ordered = sorted(
        [slot for slot in qualifying if slot.monday is not None],
        key=lambda slot: slot.monday or date.min,
    )
    latest: tuple[_Slot, _Slot] | None = None
    for left, right in zip(ordered, ordered[1:], strict=False):
        monday_left = left.monday
        monday_right = right.monday
        if monday_left is None or monday_right is None:
            continue
        diff = (monday_right - monday_left).days
        if diff == 1:
            reasons.append("one_day_apart")
        if diff == 7:
            latest = (left, right)
    return latest


def _check_bug_freshness(
    count: int | None,
    query: str,
    bug_ts: datetime | None,
    later_ts: datetime | None,
    reasons: list[str],
) -> None:
    del query
    if count is not None and count != 0:
        reasons.append("open_critical_bugs")
    if bug_ts is None:
        reasons.append("missing_bug_timestamp")
    if later_ts is None:
        reasons.append("missing_later_run_timestamp")
    if bug_ts is None or later_ts is None:
        return
    if bug_ts < later_ts:
        reasons.append("bug_count_before_later_run")
        return
    if bug_ts - later_ts > _FRESHNESS:
        reasons.append("bug_count_stale")


def _calendar_day(slot: str) -> date | None:
    match = _CAL_DATE.fullmatch(slot.strip())
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _platform_kind(raw: str) -> str | None:
    lowered = raw.strip().lower()
    if lowered.startswith("mac"):
        return "macos"
    if lowered.startswith("ubuntu") or lowered.startswith("linux"):
        return "linux"
    return None


def _is_pass(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    token = value.strip().split()[0].lower()
    return token == "pass"


def _parse_utc(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
