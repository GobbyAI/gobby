"""Usage ledger reads over token_events, scoped to one session or one task (plan 3.1, #23601).

A page is a keyset traversal of token_events rows in (event_at, session_id, message_key)
order. Totals, reported spend and coverage are recomputed on every call, so each page
describes its scope as it stands when it is read.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from gobby.storage.hub.protocol import HubDatabase

_LIMIT_MIN = 1
_LIMIT_MAX = 1000
_MESSAGE_KEY = "COALESCE(te.message_id, '#' || te.id::text)"
_NO_RUN_REASON = "no reported run"
_UNCOVERED_REASON = "source has no coverage entry"
_TOKEN_FIELDS = (
    "rows",
    "api_calls",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)
_RUN_COLUMNS = (
    "SELECT run_key, session_id, source, cost_unit, cost_amount, cost_complete,"
    " api_duration_ms, wall_duration_ms, started_at, observed_at"
)

# The plan's Provider Coverage table. Each mode is exact, reported, or unknown; an unknown mode
# carries the capability reason that its descriptor reports.
SOURCE_COVERAGE: dict[str, dict[str, dict[str, str]]] = {
    "claude": {
        "tokens": {"mode": "exact"},
        "api_calls": {"mode": "exact"},
        "spend": {"mode": "reported"},
    },
    "codex": {
        "tokens": {"mode": "exact"},
        "api_calls": {"mode": "exact"},
        "spend": {
            "mode": "unknown",
            "reason": "Codex reports an account credit balance only, shown in quota details.",
        },
    },
    "grok": {
        "tokens": {"mode": "exact"},
        "api_calls": {"mode": "reported"},
        "spend": {"mode": "reported"},
    },
    "droid": {
        "tokens": {"mode": "exact"},
        "api_calls": {"mode": "unknown", "reason": "Droid reports no per-call count."},
        "spend": {"mode": "reported"},
    },
    "qwen": {
        "tokens": {"mode": "exact"},
        "api_calls": {"mode": "unknown", "reason": "Qwen reports no per-call count."},
        "spend": {"mode": "unknown", "reason": "Qwen reports no spend."},
    },
    "agy": {
        "tokens": {"mode": "unknown", "reason": "AGY transcripts carry no usage."},
        "api_calls": {"mode": "unknown", "reason": "AGY transcripts carry no usage."},
        "spend": {"mode": "unknown", "reason": "AGY transcripts carry no usage."},
    },
}
_UNCOVERED: dict[str, dict[str, str]] = {
    "tokens": {"mode": "unknown", "reason": _UNCOVERED_REASON},
    "api_calls": {"mode": "unknown", "reason": _UNCOVERED_REASON},
    "spend": {"mode": "unknown", "reason": _UNCOVERED_REASON},
}


@dataclass(frozen=True)
class _Run:
    """One provider-reported run, as a session_reported_usage row."""

    run_key: str
    session_id: str
    source: str
    unit: str | None
    amount: Decimal | None
    complete: bool
    api_ms: int | None
    wall_ms: int | None
    started: datetime
    observed: datetime


@dataclass(frozen=True)
class _Claim:
    """One task claim interval, as a task_claim_intervals row."""

    id: str
    task_id: str
    session_id: str
    claimed: datetime
    released: datetime | None


class UsageLedgerStore:
    """Keyset-paged usage ledger for one session or one task."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def page(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return one page of ledger rows with the scope's totals, spend and coverage."""
        session_uuid, task_uuid = _scope_ids(session_id, task_id)
        size = max(_LIMIT_MIN, min(limit, _LIMIT_MAX))
        frm, conditions, scope_params = _scope_sql(session_uuid, task_uuid)
        scope_where = " AND ".join(conditions)

        where = scope_where
        params = list(scope_params)
        if cursor is not None:
            after_at, after_session, after_key = _decode_cursor(cursor)
            where += (
                f" AND (te.event_at, te.session_id, {_MESSAGE_KEY})"
                " > (%s::timestamptz, %s::uuid, %s)"
            )
            params += [after_at, after_session, after_key]
        fetched = self.db.fetchall(
            "SELECT te.event_at, te.session_id, te.message_id, te.source, te.model,"
            " te.api_calls, te.input_tokens, te.output_tokens, te.cache_creation_tokens,"
            " te.cache_read_tokens, te.metadata->>'agent_id' AS agent_id,"
            f" {_MESSAGE_KEY} AS message_key"
            f" {frm} WHERE {where}"
            f" ORDER BY te.event_at, te.session_id, {_MESSAGE_KEY} LIMIT %s",
            [*params, size + 1],
        )
        has_more = len(fetched) > size
        page_rows = fetched[:size]

        by_source, first_at, last_at = self._aggregate(frm, scope_where, scope_params)
        runs, attributed, unattributed, since, claimed_ms = self._runs(session_uuid, task_uuid)
        span_ms = None
        if session_uuid is not None and first_at is not None and last_at is not None:
            span_ms = int((last_at - first_at).total_seconds() * 1000)
        return {
            "scope": (
                {"session_id": session_uuid} if session_uuid is not None else {"task_id": task_uuid}
            ),
            "rows": [_row_out(row) for row in page_rows],
            "next_cursor": _encode_cursor(page_rows[-1]) if has_more else None,
            "totals": _totals(by_source),
            "spend": {
                "reported": _reported(attributed),
                "unattributed": [_unattributed_out(run) for run in unattributed],
            },
            "durations": {"active_span_ms": span_ms, "claimed_ms": claimed_ms},
            "attribution_since": _iso(since) if since is not None else None,
            "coverage": _coverage(by_source, runs),
        }

    def _aggregate(
        self, frm: str, scope_where: str, scope_params: list[Any]
    ) -> tuple[dict[str, dict[str, Any]], datetime | None, datetime | None]:
        """Per-source totals over the whole scope, plus its first and last event."""
        rows = self.db.fetchall(
            "SELECT te.source, COUNT(*) AS row_count, COUNT(te.api_calls) AS api_rows,"
            " COALESCE(SUM(te.api_calls), 0) AS api_calls,"
            " COALESCE(SUM(te.input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(te.output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(te.cache_creation_tokens), 0) AS cache_creation_tokens,"
            " COALESCE(SUM(te.cache_read_tokens), 0) AS cache_read_tokens,"
            " COUNT(*) FILTER (WHERE te.message_id IS NULL) AS unkeyed_rows,"
            " MIN(te.event_at) AS first_at, MAX(te.event_at) AS last_at"
            f" {frm} WHERE {scope_where} GROUP BY te.source",
            scope_params,
        )
        by_source = {
            row["source"]: {
                "rows": row["row_count"],
                "api_calls": row["api_calls"],
                "api_calls_complete": row["api_rows"] == row["row_count"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_creation_tokens": row["cache_creation_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "unkeyed_rows": row["unkeyed_rows"],
            }
            for row in rows
        }
        firsts = [row["first_at"] for row in rows if row["first_at"] is not None]
        lasts = [row["last_at"] for row in rows if row["last_at"] is not None]
        return by_source, min(firsts, default=None), max(lasts, default=None)

    def _runs(
        self, session_uuid: str | None, task_uuid: str | None
    ) -> tuple[list[_Run], list[_Run], list[_Run], datetime | None, int]:
        """Runs in scope, the attributed and unattributed split, and the task's claim summary."""
        if session_uuid is not None:
            runs = [
                _run(row)
                for row in self.db.fetchall(
                    f"{_RUN_COLUMNS} FROM session_reported_usage WHERE session_id = %s::uuid",
                    [session_uuid],
                )
            ]
            return runs, runs, [], None, 0

        assert task_uuid is not None
        runs = [
            _run(row)
            for row in self.db.fetchall(
                f"{_RUN_COLUMNS} FROM session_reported_usage"
                " WHERE session_id IN (SELECT session_id FROM task_claim_intervals"
                " WHERE task_id = %s::uuid)",
                [task_uuid],
            )
        ]
        claims = [
            _Claim(
                id=str(row["id"]),
                task_id=str(row["task_id"]),
                session_id=str(row["session_id"]),
                claimed=row["claimed_at"],
                released=row["released_at"],
            )
            for row in self.db.fetchall(
                "SELECT id, task_id, session_id, claimed_at, released_at FROM task_claim_intervals"
                " WHERE session_id IN (SELECT session_id FROM task_claim_intervals"
                " WHERE task_id = %s::uuid) ORDER BY claimed_at, id",
                [task_uuid],
            )
        ]
        summary = self.db.fetchone(
            "SELECT MIN(claimed_at) AS since,"
            " COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(released_at, now()) - claimed_at))), 0)"
            " AS seconds FROM task_claim_intervals WHERE task_id = %s::uuid",
            [task_uuid],
        )
        attributed, unattributed = _attribute(task_uuid, runs, claims)
        since = None if summary is None else summary["since"]
        seconds = 0.0 if summary is None else float(summary["seconds"])
        in_scope = [*attributed, *unattributed]
        return in_scope, attributed, unattributed, since, int(seconds * 1000)


def _scope_ids(session_id: str | None, task_id: str | None) -> tuple[str | None, str | None]:
    if (session_id is None) == (task_id is None):
        raise ValueError("exactly one of session_id and task_id is required")
    return (
        None if session_id is None else _uuid_text(session_id, "session_id"),
        None if task_id is None else _uuid_text(task_id, "task_id"),
    )


def _uuid_text(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _scope_sql(session_uuid: str | None, task_uuid: str | None) -> tuple[str, list[str], list[Any]]:
    """The FROM clause, the scope conditions, and their parameters for one scope."""
    if task_uuid is None:
        return "FROM token_events te", ["te.session_id = %s::uuid"], [session_uuid]
    # A row belongs to the task when the latest claim in its own session that covers event_at
    # belongs to the task. Only sessions that hold a claim of the task are candidates.
    frm = (
        "FROM token_events te JOIN LATERAL ("
        " SELECT i.task_id FROM task_claim_intervals i"
        " WHERE i.session_id = te.session_id AND i.claimed_at <= te.event_at"
        " AND te.event_at < COALESCE(i.released_at, 'infinity'::timestamptz)"
        " ORDER BY i.claimed_at DESC, i.id DESC LIMIT 1"
        ") latest ON latest.task_id = %s::uuid"
    )
    conditions = [
        "te.session_id IN (SELECT session_id FROM task_claim_intervals WHERE task_id = %s::uuid)"
    ]
    return frm, conditions, [task_uuid, task_uuid]


def _attribute(
    task_id: str, runs: list[_Run], claims: list[_Claim]
) -> tuple[list[_Run], list[_Run]]:
    """Split the runs that touch the task into attributed and unattributed.

    A run is attributed only when one interval of the task covers its span and no other
    interval in its session opens inside that span. Any other run that touches the task's
    intervals is unattributed, so spend is never split across tasks.
    """
    attributed: list[_Run] = []
    unattributed: list[_Run] = []
    for run in runs:
        same_session = [claim for claim in claims if claim.session_id == run.session_id]
        touching = [
            claim for claim in same_session if claim.task_id == task_id and _overlaps(claim, run)
        ]
        if not touching:
            continue
        covering = [claim for claim in touching if _covers(claim, run)]
        covering_ids = {claim.id for claim in covering}
        opened_inside = [
            claim
            for claim in same_session
            if claim.id not in covering_ids and run.started <= claim.claimed <= run.observed
        ]
        if len(covering) == 1 and not opened_inside:
            attributed.append(run)
        else:
            unattributed.append(run)
    return attributed, unattributed


def _overlaps(claim: _Claim, run: _Run) -> bool:
    return claim.claimed <= run.observed and (
        claim.released is None or claim.released >= run.started
    )


def _covers(claim: _Claim, run: _Run) -> bool:
    return claim.claimed <= run.started and (
        claim.released is None or run.observed <= claim.released
    )


def _run(row: Mapping[str, Any]) -> _Run:
    return _Run(
        run_key=row["run_key"],
        session_id=str(row["session_id"]),
        source=row["source"],
        unit=row["cost_unit"],
        amount=row["cost_amount"],
        complete=bool(row["cost_complete"]),
        api_ms=row["api_duration_ms"],
        wall_ms=row["wall_duration_ms"],
        started=row["started_at"],
        observed=row["observed_at"],
    )


def _row_out(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_at": _iso(row["event_at"]),
        "session_id": str(row["session_id"]),
        "message_id": row["message_id"],
        "source": row["source"],
        "model": row["model"],
        "api_calls": row["api_calls"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_creation_tokens": row["cache_creation_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "agent_id": row["agent_id"],
    }


def _totals(by_source: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        field: sum(stats[field] for stats in by_source.values()) for field in _TOKEN_FIELDS
    }
    totals["api_calls_complete"] = all(stats["api_calls_complete"] for stats in by_source.values())
    totals["by_source"] = {
        source: {key: value for key, value in stats.items() if key != "unkeyed_rows"}
        for source, stats in by_source.items()
    }
    return totals


def _reported(runs: list[_Run]) -> dict[str, Any]:
    return {
        "runs": len(runs),
        "incomplete_runs": sum(1 for run in runs if not run.complete),
        "by_unit": _amounts(runs),
        "api_duration_ms": _sum_ms(run.api_ms for run in runs),
        "wall_duration_ms": _sum_ms(run.wall_ms for run in runs),
    }


def _amounts(runs: Iterable[_Run]) -> dict[str, str]:
    """Provider-reported amounts summed per unit, as strings. A run without a unit adds nothing."""
    totals: dict[str, Decimal] = {}
    for run in runs:
        if run.unit is not None and run.amount is not None:
            totals[run.unit] = totals.get(run.unit, Decimal(0)) + run.amount
    return {unit: str(amount) for unit, amount in totals.items()}


def _sum_ms(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _unattributed_out(run: _Run) -> dict[str, Any]:
    return {
        "run_key": run.run_key,
        "session_id": run.session_id,
        "source": run.source,
        "unit": run.unit,
        "amount": None if run.amount is None else str(run.amount),
    }


def _coverage(by_source: Mapping[str, Mapping[str, Any]], runs: list[_Run]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for source in sorted(set(by_source) | {run.source for run in runs}):
        capability = SOURCE_COVERAGE.get(source, _UNCOVERED)
        stats = by_source.get(source)
        descriptors.append(
            {
                "source": source,
                "tokens": dict(capability["tokens"]),
                "api_calls": dict(capability["api_calls"]),
                "spend": dict(capability["spend"]),
                "spend_observed": _spend_observed(
                    capability["spend"], [run for run in runs if run.source == source]
                ),
                "unkeyed_rows": 0 if stats is None else stats["unkeyed_rows"],
            }
        )
    return descriptors


def _spend_observed(spend: Mapping[str, str], runs: list[_Run]) -> dict[str, Any]:
    """What the scope's runs show for one source. Unknown spend keeps its capability reason."""
    if spend["mode"] == "unknown":
        return {"mode": "unknown", "reason": spend["reason"]}
    if not runs:
        return {"mode": "unknown", "reason": _NO_RUN_REASON}
    return {
        "mode": "reported",
        "runs": len(runs),
        "incomplete_runs": sum(1 for run in runs if not run.complete),
        "amounts": _amounts(runs),
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _encode_cursor(row: Mapping[str, Any]) -> str:
    payload = [_iso(row["event_at"]), str(row["session_id"]), row["message_key"]]
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str, str]:
    """The (event_at, session_id, message_key) a cursor names. Anything else is malformed."""
    try:
        payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("malformed usage ledger cursor") from exc
    if not (
        isinstance(payload, list)
        and len(payload) == 3
        and all(isinstance(part, str) for part in payload)
    ):
        raise ValueError("malformed usage ledger cursor")
    event_at, session_id, message_key = payload
    try:
        return datetime.fromisoformat(event_at), _uuid_text(session_id, "cursor"), message_key
    except ValueError as exc:
        raise ValueError("malformed usage ledger cursor") from exc
