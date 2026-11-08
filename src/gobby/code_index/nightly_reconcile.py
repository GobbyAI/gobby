"""Nightly ``gcode vector reconcile`` for one project.

The run is a dry run unless the config-store apply key is on. Every run records one
receipt, whatever its outcome. A failed attempt (read error, digest race, apply error)
retries from a fresh dry run; an empty input is repaired before apply, and a repair that
leaves it empty fails the attempt instead of applying.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from gobby.code_index.gcode_gateway import GcodeCommandResult, GcodeGatewayError

MODE_DRY_RUN = "dry_run"
MODE_APPLY = "apply"
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
DEFAULT_MAX_ATTEMPTS = 3


class ReconcileGateway(Protocol):
    """The gcode calls one reconcile run makes."""

    async def vector_reconcile(
        self,
        project_root: Path,
        *,
        apply: bool = False,
        expect_digest: str | None = None,
        receipt_path: Path | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult: ...

    async def maintenance_index(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult: ...

    async def vector_rebuild(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NightlyReconcileReceipt:
    """One hub receipt row: a single reconcile run for one project."""

    run_id: str
    project_id: str
    mode: str
    digest: str | None
    class_counts: dict[str, Any]
    outcome: str
    retries: int
    error: str | None
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class _Observation:
    """The digest and counts one dry run reported."""

    digest: str
    points_scanned: int
    pg_symbols: int
    class_counts: dict[str, Any]


class _AttemptFailedError(Exception):
    """One attempt failed; the run may retry it."""


async def reconcile_project(
    *,
    run_id: str,
    project_id: str,
    root: Path,
    gateway: ReconcileGateway,
    apply_enabled: bool,
    receipt_dir: Path,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> NightlyReconcileReceipt:
    """Reconcile one project and return the receipt for the run."""
    started_at = _now()
    mode = MODE_APPLY if apply_enabled else MODE_DRY_RUN
    limit = max(1, max_attempts)
    last: _Observation | None = None
    attempts = 0
    while True:
        attempts += 1
        try:
            last = await _observe(gateway, root, guarded=apply_enabled, timeout=timeout, env=env)
            if apply_enabled:
                await _apply(
                    gateway,
                    root,
                    last,
                    # One receipt per attempt: gcode refuses a receipt written for an earlier
                    # inventory digest, and a retry re-observes the inventory.
                    receipt_dir / f"{run_id}-{project_id}-attempt-{attempts}.json",
                    timeout=timeout,
                    env=env,
                )
        except (_AttemptFailedError, GcodeGatewayError, OSError) as exc:
            if attempts < limit:
                continue
            return _receipt(
                run_id=run_id,
                project_id=project_id,
                mode=mode,
                observed=last,
                outcome=OUTCOME_FAILED,
                retries=attempts - 1,
                error=str(exc),
                started_at=started_at,
            )
        return _receipt(
            run_id=run_id,
            project_id=project_id,
            mode=mode,
            observed=last,
            outcome=OUTCOME_COMPLETED,
            retries=attempts - 1,
            error=None,
            started_at=started_at,
        )


async def _observe(
    gateway: ReconcileGateway,
    root: Path,
    *,
    guarded: bool,
    timeout: float | None,
    env: Mapping[str, str] | None,
) -> _Observation:
    """Dry-run the project. In apply mode, repair an empty input before it can be deleted."""
    observed = await _dry_run(gateway, root, timeout=timeout, env=env)
    if not guarded:
        return observed
    if observed.pg_symbols == 0:
        _check_result(await gateway.maintenance_index(root, timeout=timeout, env=env), "index")
        observed = await _dry_run(gateway, root, timeout=timeout, env=env)
    if observed.points_scanned == 0:
        _check_rebuild(await gateway.vector_rebuild(root, timeout=timeout, env=env))
        observed = await _dry_run(gateway, root, timeout=timeout, env=env)
    if observed.pg_symbols == 0 or observed.points_scanned == 0:
        raise _AttemptFailedError("input still empty after the repair; not applying")
    return observed


async def _dry_run(
    gateway: ReconcileGateway,
    root: Path,
    *,
    timeout: float | None,
    env: Mapping[str, str] | None,
) -> _Observation:
    payload = _payload(await gateway.vector_reconcile(root, timeout=timeout, env=env), "dry run")
    return _observation(payload)


async def _apply(
    gateway: ReconcileGateway,
    root: Path,
    observed: _Observation,
    receipt_path: Path,
    *,
    timeout: float | None,
    env: Mapping[str, str] | None,
) -> None:
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = await gateway.vector_reconcile(
        root,
        apply=True,
        expect_digest=observed.digest,
        receipt_path=receipt_path,
        timeout=timeout,
        env=env,
    )
    if _payload(result, "apply").get("complete") is not True:
        raise _AttemptFailedError("apply: gcode did not report a complete apply")


def _payload(result: GcodeCommandResult, stage: str) -> dict[str, Any]:
    """Return a successful gcode JSON object, or raise so the attempt retries."""
    if not result.success:
        detail = "timed out" if result.timed_out else _failure_text(result)
        raise _AttemptFailedError(f"{stage}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise _AttemptFailedError(f"{stage}: output is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise _AttemptFailedError(f"{stage}: gcode did not report success")
    return payload


def _failure_text(result: GcodeCommandResult) -> str:
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"


def _check_result(result: GcodeCommandResult, stage: str) -> None:
    if not result.success:
        raise _AttemptFailedError(f"{stage}: {_failure_text(result)}")


def _check_rebuild(payload: Mapping[str, Any]) -> None:
    if payload.get("success") is not True:
        raise _AttemptFailedError("vector rebuild did not succeed")


def _observation(payload: Mapping[str, Any]) -> _Observation:
    digest = payload.get("inventory_digest")
    points = payload.get("points_scanned")
    symbols = payload.get("pg_symbols")
    if not isinstance(digest, str) or not isinstance(points, int) or not isinstance(symbols, int):
        raise _AttemptFailedError("dry run: output is missing the inventory counts")
    return _Observation(
        digest=digest,
        points_scanned=points,
        pg_symbols=symbols,
        class_counts={
            "points_scanned": points,
            "pg_symbols": symbols,
            "orphans": _class_counts(payload.get("orphans")),
            "missing": _class_counts(payload.get("missing")),
        },
    )


def _class_counts(entries: object) -> dict[str, Any]:
    """Keep each class's figures and drop its id lists (reconcile.rs json_report).

    An orphan class is ``{"count", "ids"}`` and collapses to its count. A missing class is
    ``{"versions", "symbols", "version_ids"}`` and keeps its versions and symbols.
    """
    if not isinstance(entries, dict):
        return {}
    return {name: _class_figures(entry) for name, entry in entries.items()}


def _class_figures(entry: object) -> Any:
    if not isinstance(entry, dict):
        return entry
    if "count" in entry:
        return entry["count"]
    return {key: value for key, value in entry.items() if not isinstance(value, list)}


def _receipt(
    *,
    run_id: str,
    project_id: str,
    mode: str,
    observed: _Observation | None,
    outcome: str,
    retries: int,
    error: str | None,
    started_at: str,
) -> NightlyReconcileReceipt:
    return NightlyReconcileReceipt(
        run_id=run_id,
        project_id=project_id,
        mode=mode,
        digest=observed.digest if observed is not None else None,
        class_counts=dict(observed.class_counts) if observed is not None else {},
        outcome=outcome,
        retries=retries,
        error=error,
        started_at=started_at,
        finished_at=_now(),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
