"""Provider-reported run totals (plan 2.3).

Providers report what a run cost outside the per-message usage. Claude writes cumulative
``cost-state`` lines, which the parser skips. Grok attaches a cost to each ``turn_completed``
update. Droid writes a cumulative sidecar beside its transcript. Each reading becomes one
``session_reported_usage`` row keyed by run, and a newer reading of a run replaces the older.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.grok import _TURN_COMPLETED_UPDATE, _extract_update

CLAUDE_SOURCE = "claude"
GROK_SOURCE = "grok"
DROID_SOURCE = "droid"
DROID_RUN_KEY = "droid:session"
_COST_STATE = "cost-state"
_GROK_TICKS_PER_USD = Decimal(10) ** 10
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_BIGINT = 2**63 - 1  # api_duration_ms and wall_duration_ms are bigint columns


@dataclass(frozen=True)
class ReportedRun:
    """One provider-reported run total, mirroring a ``session_reported_usage`` row."""

    run_key: str
    source: str
    cost_unit: Literal["usd", "factory_credits"] | None
    cost_amount: Decimal | None
    cost_complete: bool
    api_duration_ms: int | None
    wall_duration_ms: int | None
    started_at: datetime
    observed_at: datetime
    model_usage: Mapping[str, Any] = field(default_factory=dict)


def claude_cost_runs(lines: Iterable[str]) -> list[ReportedRun]:
    """One run per Claude process start, from its latest cumulative ``cost-state`` reading.

    The reading with the largest ``observed_at`` wins. On a tie the earlier line keeps the
    run, which is the rule the strict upsert applies to a replayed reading as well.
    """
    latest: dict[int, ReportedRun] = {}
    for raw in lines:
        if _COST_STATE not in raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != _COST_STATE:
            continue
        start_ms = _number(record.get("startTime"))
        cost = _cost(record.get("totalCostUSD"))
        if start_ms is None or cost is None:
            continue
        start = int(start_ms)
        try:
            run = _claude_run(record, start, cost)
        except OverflowError:
            # A timestamp beyond the datetime range is not a reading; skip it, keep the pass.
            continue
        current = latest.get(start)
        if current is None or run.observed_at > current.observed_at:
            latest[start] = run
    return list(latest.values())


def _claude_run(record: Mapping[str, Any], start_ms: int, cost: int | float) -> ReportedRun:
    started = _EPOCH + timedelta(milliseconds=start_ms)
    wall = _duration_ms(record.get("totalDuration"))
    try:
        observed = started + timedelta(milliseconds=wall or 0)
    except OverflowError:
        # A duration that places the reading beyond the calendar is no duration; the cost stands.
        wall, observed = None, started
    usage = record.get("modelUsage")
    return ReportedRun(
        run_key=f"{CLAUDE_SOURCE}:{start_ms}",
        source=CLAUDE_SOURCE,
        cost_unit="usd",
        cost_amount=Decimal(str(cost)),
        cost_complete=record.get("hasUnknownModelCost") is not True,
        api_duration_ms=_duration_ms(record.get("totalAPIDuration")),
        wall_duration_ms=wall,
        started_at=started,
        observed_at=observed,
        model_usage=usage if isinstance(usage, dict) else {},
    )


def grok_turn_runs(records: Iterable[object]) -> list[ReportedRun]:
    """One run per completed Grok turn that carries a usage object."""
    runs: list[ReportedRun] = []
    for record in records:
        if not isinstance(record, ParsedMessage):
            continue
        if record.content_type != _TURN_COMPLETED_UPDATE or record.message_id is None:
            continue
        update = _extract_update(record.raw_json)
        usage = update.get("usage") if update is not None else None
        if not isinstance(usage, dict):
            continue
        ticks = _cost(usage.get("costUsdTicks"))
        amount = None if ticks is None else Decimal(str(ticks)) / _GROK_TICKS_PER_USD
        model_usage = usage.get("modelUsage")
        runs.append(
            ReportedRun(
                run_key=f"{GROK_SOURCE}:{record.message_id}",
                source=GROK_SOURCE,
                cost_unit=None if amount is None else "usd",
                cost_amount=amount,
                cost_complete=usage.get("usageIsIncomplete") is not True,
                api_duration_ms=_duration_ms(usage.get("apiDurationMs")),
                wall_duration_ms=None,
                started_at=record.timestamp,
                observed_at=record.timestamp,
                model_usage=model_usage if isinstance(model_usage, dict) else {},
            )
        )
    return runs


def droid_sidecar_run(transcript_path: str | Path, started_at: datetime) -> ReportedRun | None:
    """The cumulative Droid run read from ``<transcript>.settings.json``, or None when absent.

    ``observed_at`` is the sidecar's mtime, clamped to ``started_at`` so the row satisfies
    ``observed_at >= started_at`` even when the sidecar predates the session record.
    """
    sidecar = Path(transcript_path).with_suffix(".settings.json")
    try:
        mtime = sidecar.stat().st_mtime
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("tokenUsage")
    credits = _cost(usage.get("factoryCredits")) if isinstance(usage, dict) else None
    if credits is None:
        return None
    return ReportedRun(
        run_key=DROID_RUN_KEY,
        source=DROID_SOURCE,
        cost_unit="factory_credits",
        cost_amount=Decimal(str(credits)),
        cost_complete=True,
        api_duration_ms=None,
        wall_duration_ms=_duration_ms(data.get("assistantActiveTimeMs")),
        started_at=started_at,
        observed_at=max(datetime.fromtimestamp(mtime, UTC), started_at),
    )


def collect_runs(
    source: str,
    *,
    lines: Iterable[str],
    messages: Iterable[object],
    transcript_path: str | Path | None,
    session_started_at: datetime | None,
) -> list[ReportedRun]:
    """The run totals a source reports, from the transcript lines and parsed records given."""
    if source == CLAUDE_SOURCE:
        return claude_cost_runs(lines)
    if source == GROK_SOURCE:
        return grok_turn_runs(messages)
    if source == DROID_SOURCE:
        if transcript_path is None or session_started_at is None:
            return []
        run = droid_sidecar_run(transcript_path, session_started_at)
        return [] if run is None else [run]
    return []


def _number(value: object) -> int | float | None:
    """A finite real number, or None. JSON admits NaN and Infinity, which are not readings."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _cost(value: object) -> int | float | None:
    """A finite, non-negative cost, or None."""
    number = _number(value)
    return None if number is None or number < 0 else number


def _duration_ms(value: object) -> int | None:
    number = _number(value)
    if number is None or number < 0 or number > _MAX_BIGINT:
        return None
    return int(number)
