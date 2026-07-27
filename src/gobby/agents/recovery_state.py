"""Shared predicates for durable daemon-stop recovery state."""

from __future__ import annotations

from typing import Any

_PROVISIONAL_PHASES = frozenset({"prepared", "launch_requested", "runtime_persisted"})


def _metadata(run: Any) -> dict[str, Any]:
    value = getattr(run, "resume_metadata_json", None)
    return value if isinstance(value, dict) else {}


def daemon_resume_successor_id(run: Any) -> str | None:
    """Return the durable successor pointer recorded on a consumed run."""
    value = _metadata(run).get("daemon_stop_resume_consumed_by_run_id")
    return value if isinstance(value, str) and value else None


def is_daemon_stop_parked(run: Any) -> bool:
    """Return whether a terminal daemon-stop original still awaits recovery."""
    metadata = _metadata(run)
    return (
        getattr(run, "status", None) == "cancelled"
        and getattr(run, "terminal_reason", None) == "daemon_stop"
        and not metadata.get("daemon_stop_resume_consumed_at")
        and not metadata.get("daemon_stop_orphan_reaped_at")
    )


def is_reconciliation_pending(run: Any) -> bool:
    """Return whether boot classification is deferred for unresolved hook ingress."""
    return _metadata(run).get("reconciliation_pending") is True


def is_provisional_daemon_resume(run: Any) -> bool:
    """Return whether a successor handoff has not reached finalization."""
    return _metadata(run).get("daemon_stop_resume_phase") in _PROVISIONAL_PHASES


def is_recovery_protected(run: Any) -> bool:
    """Exclude unresolved recovery state from generic health termination."""
    return is_reconciliation_pending(run) or is_provisional_daemon_resume(run)
