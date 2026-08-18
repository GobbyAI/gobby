"""Memory dream admission and run lifecycle storage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from psycopg.errors import UniqueViolation

from gobby.memory.dream.options import dream_scope_key, normalize_dream_options
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import older_than_now_expr
from gobby.utils.json_helpers import json_dumps

PLATFORM_TRUTH_SCOPE = "__gobby_platform__"

# Run status vocabulary: 'running' marks the admitted run and is held by at
# most one row at a time, enforced by the partial unique index
# idx_memory_dream_runs_single_running. 'started' is the non-terminal status
# of subordinate per-target rows created under an admitted aggregate run;
# they never compete for admission.
RUN_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "reverted",
        "revert_failed",
        "revert_forfeited",
        "interrupted",
        "partial",
    }
)

_ADMISSION_ATTEMPTS = 3
_RUN_JSON_COLUMNS = frozenset({"options", "plan", "summary", "checkpoint"})
_RUN_UPDATE_SET_CLAUSES = {
    "project_id": "project_id = %s",
    "status": "status = %s",
    "dry_run": "dry_run = %s",
    "options": "options = %s",
    "plan": "plan = %s",
    "summary": "summary = %s",
    "checkpoint": "checkpoint = %s",
    "error": "error = %s",
    "started_at": "started_at = %s",
    "completed_at": "completed_at = %s",
    "reverted_at": "reverted_at = %s",
    "created_at": "created_at = %s",
    "updated_at": "updated_at = %s",
}

# Error recorded on runs reconciled to 'interrupted' after a daemon restart.
INTERRUPTED_RESTART_ERROR = "Interrupted: daemon restarted while the dream run was in progress"
# Error recorded when an in-flight run is cancelled (shutdown/timeout) before completing.
INTERRUPTED_CANCELLED_ERROR = "Interrupted: dream run cancelled before completion"


@dataclass(frozen=True, slots=True)
class DreamAdmission:
    """Outcome of one atomic run-admission attempt."""

    outcome: Literal["admitted", "coalesced", "conflict"]
    run_id: str | None
    active: dict[str, Any] | None = None


class _DreamRunHost(Protocol):
    db: HubDatabase

    def create_run(
        self,
        *,
        project_id: str | None,
        dry_run: bool,
        options: dict[str, Any],
        status: Literal["running", "started"] = "running",
    ) -> str: ...

    def get_active_run(self) -> dict[str, Any] | None: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def get_truth_digest_hash(self, project_id: str) -> str | None: ...

    def set_truth_digest_hash(self, project_id: str, digest_hash: str) -> None: ...

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None: ...

    def _resolve_against_holder(
        self,
        active: dict[str, Any],
        request: dict[str, Any],
    ) -> DreamAdmission: ...


class _DreamRunMixin:
    def create_run(
        self: _DreamRunHost,
        *,
        project_id: str | None,
        dry_run: bool,
        options: dict[str, Any],
        status: Literal["running", "started"] = "running",
    ) -> str:
        run_id = str(uuid4())
        now = _now()
        self.db.execute(
            """
            INSERT INTO memory_dream_runs (
                id, project_id, status, dry_run, options, started_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (run_id, project_id, status, dry_run, _json(options), now),
        )
        return run_id

    def admit_run(
        self: _DreamRunHost,
        *,
        project_id: str | None,
        dry_run: bool,
        options: dict[str, Any],
    ) -> DreamAdmission:
        """Atomically admit, coalesce, or refuse a run against the sole running row.

        The partial unique index on ``status = 'running'`` is the arbiter: a
        raced insert surfaces as a unique violation, after which the holder is
        re-read and the request resolves to coalesced or conflict exactly as if
        the holder had been observed first.
        """
        request = normalize_dream_options(options)
        for _ in range(_ADMISSION_ATTEMPTS):
            active = self.get_active_run()
            if active is not None:
                return self._resolve_against_holder(active, request)
            try:
                run_id = self.create_run(project_id=project_id, dry_run=dry_run, options=options)
            except UniqueViolation:
                # Raced another admission; re-read the holder on the next pass.
                continue
            return DreamAdmission(outcome="admitted", run_id=run_id)
        raise RuntimeError(
            f"memory dream admission did not converge after {_ADMISSION_ATTEMPTS} attempts"
        )

    def _resolve_against_holder(
        self: _DreamRunHost,
        active: dict[str, Any],
        request: dict[str, Any],
    ) -> DreamAdmission:
        view = _admission_view(active)
        if _covers(normalize_dream_options(active.get("options") or {}), request):
            return DreamAdmission(outcome="coalesced", run_id=view["run_id"], active=view)
        return DreamAdmission(outcome="conflict", run_id=None, active=view)

    def get_active_run(self: _DreamRunHost) -> dict[str, Any] | None:
        """Return the sole 'running' row, decoded, or None."""
        row = self.db.fetchone(
            "SELECT * FROM memory_dream_runs WHERE status = 'running' LIMIT 1",
            (),
        )
        return None if row is None else _decode_run_row(row)

    def update_run(
        self: _DreamRunHost,
        run_id: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        if not fields:
            return self.get_run(run_id)
        unknown_fields = sorted(set(fields) - set(_RUN_UPDATE_SET_CLAUSES))
        if unknown_fields:
            raise ValueError(
                "Unsupported memory_dream_runs update field(s): " + ", ".join(unknown_fields)
            )
        fields["updated_at"] = _now()
        encoded = {
            key: _json(value) if key in _RUN_JSON_COLUMNS else value
            for key, value in fields.items()
        }
        # Column assignments are selected from _RUN_UPDATE_SET_CLAUSES above;
        # values remain parameterized with psycopg placeholders.
        set_clause = ", ".join(_RUN_UPDATE_SET_CLAUSES[key] for key in encoded)
        self.db.execute(
            f"UPDATE memory_dream_runs SET {set_clause} WHERE id = %s",  # nosec B608
            tuple(encoded.values()) + (run_id,),
        )
        return self.get_run(run_id)

    def get_run(
        self: _DreamRunHost,
        run_id: str,
    ) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM memory_dream_runs WHERE id = %s", (run_id,))
        return None if row is None else _decode_run_row(row)

    def get_truth_digest_hash(
        self: _DreamRunHost,
        project_id: str,
    ) -> str | None:
        """Return the last-seen codewiki truth-digest hash for a project."""
        row = self.db.fetchone(
            "SELECT digest_hash FROM memory_dream_truth_state WHERE project_id = %s",
            (project_id,),
        )
        return str(row["digest_hash"]) if row is not None else None

    def get_platform_truth_digest_hash(self: _DreamRunHost) -> str | None:
        """Return the last-seen platform truth digest hash."""
        return self.get_truth_digest_hash(PLATFORM_TRUTH_SCOPE)

    def set_truth_digest_hash(
        self: _DreamRunHost,
        project_id: str,
        digest_hash: str,
    ) -> None:
        """Record the current codewiki truth-digest hash for a project."""
        self.db.execute(
            """
            INSERT INTO memory_dream_truth_state (project_id, digest_hash, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (project_id) DO UPDATE
                SET digest_hash = EXCLUDED.digest_hash,
                    updated_at = EXCLUDED.updated_at
            """,
            (project_id, digest_hash),
        )

    def set_platform_truth_digest_hash(
        self: _DreamRunHost,
        digest_hash: str,
    ) -> None:
        """Record the current platform truth digest hash."""
        self.set_truth_digest_hash(PLATFORM_TRUTH_SCOPE, digest_hash)

    def mark_interrupted_runs(
        self: _DreamRunHost,
        *,
        error: str = INTERRUPTED_RESTART_ERROR,
    ) -> list[str]:
        """Reconcile runs orphaned in a non-terminal state to 'interrupted'.

        A dream run executes as an in-process asyncio background task with no
        external liveness handle, so a daemon restart cancels it without
        persisting a terminal status, leaving the row at 'running'/'started'.
        The caller must invoke this once during synchronous startup, before the
        HTTP server accepts requests or any new run is scheduled, so every
        non-terminal row is necessarily orphaned and no live run is clobbered.

        Returns the reconciled run IDs.
        """
        rows = self.db.fetchall(
            "SELECT id FROM memory_dream_runs WHERE status IN ('started', 'running')"
        )
        run_ids = [str(row["id"]) for row in rows]
        completed_at = _now()
        for run_id in run_ids:
            self.update_run(
                run_id,
                status="interrupted",
                completed_at=completed_at,
                error=error,
            )
        return run_ids

    def prune_runs(
        self: _DreamRunHost,
        older_than_days: int,
    ) -> int:
        """Delete dream runs older than the retention window.

        Snapshots are reclaimed automatically: ``memory_dream_snapshots.run_id``
        carries an ``ON DELETE CASCADE`` foreign key, so removing aged runs drops
        their snapshot rows in the same statement. Returns the run count removed.
        """
        if older_than_days <= 0:
            raise ValueError("older_than_days must be positive")
        cutoff = older_than_now_expr(self.db, "created_at", "%s", "day")
        terminal_statuses = tuple(sorted(RUN_TERMINAL_STATUSES))
        status_placeholders = ", ".join("%s" for _ in terminal_statuses)
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""DELETE FROM memory_dream_runs
                     WHERE status IN ({status_placeholders})
                       AND {cutoff}
                 RETURNING id""",  # nosec B608
                (*terminal_statuses, older_than_days),
            ).fetchall()
        return len(rows)


def _covers(active: dict[str, Any], request: dict[str, Any]) -> bool:
    """Whether the active run's normalized options cover the request.

    Equivalent options coalesce. An all-due run covers a project request when
    the four shared flags match and the request does not narrow
    ``include_global`` incompatibly (``include_global=False`` demands a sweep
    that excludes the global bucket, which the all-due run does not honor).
    Project runs cover only the same project and options; everything else
    conflicts.
    """
    if active == request:
        return True
    shared_flags = ("dry_run", "skip_consolidation", "memory_type", "full_sweep")
    return (
        active["project_id"] is None
        and not active["global_only"]
        and request["project_id"] is not None
        and not request["global_only"]
        and all(active[flag] == request[flag] for flag in shared_flags)
        and request["include_global"] is not False
    )


def _decode_run_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    for key in ("options", "plan", "summary", "checkpoint"):
        data[key] = _decode(data.get(key))
    return data


def _admission_view(run: Mapping[str, Any]) -> dict[str, Any]:
    """Active-run details returned on coalesced and conflicting admissions."""
    options = run.get("options") or {}
    checkpoint = run.get("checkpoint")
    phase = checkpoint.get("phase") if isinstance(checkpoint, dict) else None
    return {
        "run_id": str(run["id"]),
        "scope": dream_scope_key(options),
        "options": normalize_dream_options(options),
        "phase": phase or str(run.get("status")),
        "checkpoint": checkpoint,
    }


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json_dumps(value)


def _decode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()
