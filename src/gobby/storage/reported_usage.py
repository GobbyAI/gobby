"""Storage for provider-reported run totals (plan 2.3).

A reading is cumulative, so an upsert keeps the newer one. A replay, or a reading the row
already holds, matches no update, so the row version (xmin) stays put and idle polls write
nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from gobby.sessions.reported_usage import ReportedRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.token_events import token_event_retention_transaction

_UPSERT_SQL = """
    INSERT INTO session_reported_usage (
        session_id, run_key, source, cost_unit, cost_amount, cost_complete,
        api_duration_ms, wall_duration_ms, started_at, observed_at, model_usage
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (session_id, run_key) DO UPDATE SET
        source = excluded.source,
        cost_unit = excluded.cost_unit,
        cost_amount = excluded.cost_amount,
        cost_complete = excluded.cost_complete,
        api_duration_ms = excluded.api_duration_ms,
        wall_duration_ms = excluded.wall_duration_ms,
        started_at = excluded.started_at,
        observed_at = excluded.observed_at,
        model_usage = excluded.model_usage
    WHERE session_reported_usage.observed_at < excluded.observed_at
"""

_LIST_SQL = """
    SELECT run_key, source, cost_unit, cost_amount, cost_complete, api_duration_ms,
           wall_duration_ms, started_at, observed_at, model_usage
    FROM session_reported_usage
    WHERE session_id = %s
    ORDER BY run_key
"""


class ReportedUsageStore:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def upsert_runs(self, session_id: str, runs: Sequence[ReportedRun]) -> None:
        """Write each run unless the row already holds an equal or newer reading."""
        if not runs:
            return
        with token_event_retention_transaction(self.db):
            for run in runs:
                self.db.execute(
                    _UPSERT_SQL,
                    (
                        session_id,
                        run.run_key,
                        run.source,
                        run.cost_unit,
                        run.cost_amount,
                        run.cost_complete,
                        run.api_duration_ms,
                        run.wall_duration_ms,
                        run.started_at,
                        run.observed_at,
                        json.dumps(dict(run.model_usage)),
                    ),
                )

    def delete_runs(self, session_id: str, run_keys: Sequence[str]) -> None:
        """Remove stored runs that the transcript no longer produces."""
        if not run_keys:
            return
        self.db.execute(
            "DELETE FROM session_reported_usage WHERE session_id = %s AND run_key = ANY(%s)",
            (session_id, list(run_keys)),
        )

    def list_runs(self, session_id: str) -> list[ReportedRun]:
        return [_row_to_run(row) for row in self.db.fetchall(_LIST_SQL, (session_id,))]


def _row_to_run(row: Mapping[str, Any]) -> ReportedRun:
    model_usage = row["model_usage"]
    if isinstance(model_usage, str):
        model_usage = json.loads(model_usage)
    return ReportedRun(
        run_key=row["run_key"],
        source=row["source"],
        cost_unit=row["cost_unit"],
        cost_amount=row["cost_amount"],
        cost_complete=row["cost_complete"],
        api_duration_ms=row["api_duration_ms"],
        wall_duration_ms=row["wall_duration_ms"],
        started_at=row["started_at"],
        observed_at=row["observed_at"],
        model_usage=model_usage if isinstance(model_usage, dict) else {},
    )
