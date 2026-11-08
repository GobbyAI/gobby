"""Nightly ``gcode vector reconcile`` for one project.

The run is a dry run unless the config-store apply key is on. Every run records one
receipt, whatever its outcome.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gobby.code_index.gcode_gateway import GcodeCommandResult

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

    async def vector_rebuild(self, project_root: Path) -> dict[str, Any]: ...


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
    raise NotImplementedError
