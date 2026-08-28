"""Shared metadata keys and SQL predicates for daemon-stop resume state.

Every reader of the consumption state must use these helpers so that the
"consumed" predicate cannot drift between call sites: an original run is
consumed only when `daemon_stop_resume_consumed_at` holds a non-empty value.
"""

from __future__ import annotations

from gobby.storage.sql_dialect import json_text_expr

RESUME_PHASE_KEY = "daemon_stop_resume_phase"
CONSUMED_AT_KEY = "daemon_stop_resume_consumed_at"
CONSUMED_BY_KEY = "daemon_stop_resume_consumed_by_run_id"
FAILURE_COUNT_KEY = "daemon_stop_resume_failure_count"
REAP_STARTED_AT_KEY = "daemon_stop_orphan_reap_started_at"
REAP_REQUESTED_AT_KEY = "daemon_stop_orphan_reap_requested_at"
REAPED_AT_KEY = "daemon_stop_orphan_reaped_at"
RECONCILIATION_PENDING_KEY = "reconciliation_pending"
FINALIZED_AT_KEY = "daemon_stop_resume_finalized_at"
TERMINAL_ID_KEY = "daemon_stop_resume_terminal_id"
SPAWN_KEY_KEY = "daemon_stop_resume_spawn_key"
RESUMED_FROM_RUN_ID_KEY = "resumed_from_run_id"
_RESUME_METADATA_COLUMNS = frozenset({"ar.resume_metadata_json"})


def _validate_resume_metadata_column(column: str) -> None:
    if column not in _RESUME_METADATA_COLUMNS:
        raise ValueError(f"Unsupported daemon resume metadata column: {column}")


def daemon_resume_unconsumed_condition(db: object, column: str) -> str:
    """SQL condition matching parked originals not yet consumed by a successor."""
    _validate_resume_metadata_column(column)
    expr = json_text_expr(db, column, CONSUMED_AT_KEY)
    return f"({expr} IS NULL OR {expr} = '')"


def daemon_resume_consumed_condition(db: object, column: str) -> str:
    """SQL condition matching originals already consumed by a successor."""
    _validate_resume_metadata_column(column)
    expr = json_text_expr(db, column, CONSUMED_AT_KEY)
    return f"({expr} IS NOT NULL AND {expr} <> '')"
