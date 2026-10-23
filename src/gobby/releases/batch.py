"""Release batch: validate, activate and smoke the release window's pinned candidate.

One batch runs as a long-lived CLI process because it outlives the daemon it
restarts. A keeper renews the window through the hub database while every
synchronous step runs in a worker thread. Each batch writes one JSON record under
``<git-common-dir>/gobby/release-batches/``; the newest succeeded record's
candidate is the release baseline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from gobby.releases.smoke import SmokeResult
from gobby.releases.window import RENEW_INTERVAL

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

ActivationKind = Literal["cutover", "restart"]


@dataclass(frozen=True)
class Check:
    name: str
    command: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    command: str
    success: bool
    output: str


class ActivationFailed(RuntimeError):
    """The cutover or restart did not complete."""


class ReleaseBatchRefused(Exception):
    """The batch refused before its first renewal and left the window untouched.

    Codes: ``release_baseline_missing``, ``release_base_not_ancestor`` and
    ``release_candidate_unpinned``.
    """

    code: str

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _wall_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BatchEnvironment:
    """Synchronous ports. The batch runs each one in a worker thread."""

    command: Callable[[str], str | None]
    run_check: Callable[[Check], CheckResult]
    activation_kind: Callable[[], ActivationKind]
    activate: Callable[[ActivationKind], None]
    capture: Callable[[], frozenset[str]]
    smoke: Callable[[frozenset[str]], list[SmokeResult]]
    announce: Callable[[str], str | None]
    clock: Callable[[], datetime] = _wall_clock
    renew_every: float = RENEW_INTERVAL.total_seconds()


@dataclass(frozen=True)
class BatchRecord:
    base_sha: str
    candidate_sha: str
    token: str
    started_at: str
    check_plan: tuple[Check, ...] = ()
    results: tuple[CheckResult, ...] = ()
    activation: ActivationKind | None = None
    smokes: tuple[SmokeResult, ...] = ()
    status: Literal["running", "succeeded", "failed"] = "running"
    phase: Literal["checks", "activation", "smokes", "lease"] | None = None
    diagnostics: tuple[str, ...] = field(default=())
    regression_task: str | None = None


def records_dir(git_common_dir: str | Path) -> Path:
    raise NotImplementedError


def latest_baseline(git_common_dir: str | Path) -> str | None:
    """Return the candidate of the newest succeeded record, or None."""
    raise NotImplementedError


async def run_batch(
    db: HubDatabase,
    *,
    project_id: str,
    owner_session_id: str,
    token: str,
    generation: int,
    base_sha: str | None,
    env: BatchEnvironment,
) -> BatchRecord:
    """Run one batch for the caller's live window and return its final record."""
    raise NotImplementedError
