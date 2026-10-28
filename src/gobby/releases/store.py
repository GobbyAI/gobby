"""Release lease store: the hub's fenced view of the release window.

``HubReleaseLease`` runs where the hub database and the release window live. Regression
filing reuses the open task for a candidate or creates one, as the Release Manager session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.agents.recovery_state import is_reconciliation_pending
from gobby.releases.batch import Checkout, Phase
from gobby.releases.window import (
    ReleaseWindow,
    project_checkout_root,
    release_window,
    renew_window,
    require_lease,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

_RUN_PAGE = 500
_TAIL = 2000


def _now() -> datetime:
    return datetime.now(UTC)


def _regression_label(candidate_sha: str) -> str:
    return f"release-regression-{candidate_sha[:12]}"


def _regression_description(
    candidate_sha: str, base_sha: str, phase: Phase, diagnostic: str
) -> str:
    return (
        f"Release batch for candidate {candidate_sha} (base {base_sha}) "
        f"failed in phase {phase}.\n\n{diagnostic[-_TAIL:]}"
    )


def _regression_criteria(candidate_sha: str, phase: Phase) -> str:
    return (
        f"The {phase} failure recorded for {candidate_sha[:12]} is fixed, and a release batch "
        "for a candidate containing the fix records status succeeded."
    )


def _active_runs(db: HubDatabase) -> frozenset[str]:
    """The machine's active runs that are not already reconciliation-fenced. Blocking."""
    machine_id = require_machine_id()
    runs = LocalAgentRunManager(db)
    found: set[str] = set()
    offset = 0
    while page := runs.list_active_for_machine(machine_id, limit=_RUN_PAGE, offset=offset):
        found.update(run.id for run in page if not is_reconciliation_pending(run))
        if len(page) < _RUN_PAGE:
            break
        offset += len(page)
    return frozenset(found)


def _fenced_runs(db: HubDatabase, run_ids: frozenset[str]) -> frozenset[str]:
    runs = LocalAgentRunManager(db)
    return frozenset(
        run_id
        for run_id in run_ids
        if (run := runs.get(run_id)) is not None and is_reconciliation_pending(run)
    )


def _hub_regression_task(
    db: HubDatabase,
    project_id: str,
    owner_session_id: str,
    candidate_sha: str,
    base_sha: str,
    phase: Phase,
    diagnostic: str,
) -> str:
    """Find or create the candidate's one open regression task, through the hub."""
    label = _regression_label(candidate_sha)
    tasks = LocalTaskManager(db)
    if existing := tasks.list_tasks(project_id=project_id, label=label, closed=False, limit=1):
        return existing[0].id
    task = tasks.create_task(
        project_id,
        f"Release regression in {candidate_sha[:12]}",
        _regression_description(candidate_sha, base_sha, phase, diagnostic),
        created_in_session_id=owner_session_id,
        priority=1,
        task_type="bug",
        labels=[label],
        category="code",
        validation_criteria=_regression_criteria(candidate_sha, phase),
    )
    return task.id


@dataclass
class HubReleaseLease:
    """The release lease over the hub database, fenced by owner, token and generation."""

    db: HubDatabase
    project_id: str
    owner_session_id: str
    token: str
    generation: int
    clock: Callable[[], datetime] = _now
    # A renewal advances the generation the next call sends; the lock keeps the two in step.
    _fence: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def require_live(self) -> ReleaseWindow:
        """Return the caller's live window, or refuse ``release_window_stale``."""
        return await require_lease(
            self.db,
            project_id=self.project_id,
            owner_session_id=self.owner_session_id,
            token=self.token,
            generation=self.generation,
            now=self.clock(),
        )

    async def checkout(self) -> Checkout:
        async with self._fence:
            window = await self.require_live()
            root = await asyncio.to_thread(project_checkout_root, self.db, self.project_id)
        return Checkout(Path(root), window.candidate_sha)

    async def renew(self) -> None:
        async with self._fence:
            renewed = await renew_window(
                self.db,
                project_id=self.project_id,
                owner_session_id=self.owner_session_id,
                token=self.token,
                generation=self.generation,
                now=self.clock(),
            )
            self.generation = renewed.generation

    async def release(self) -> None:
        async with self._fence:
            await release_window(
                self.db,
                project_id=self.project_id,
                owner_session_id=self.owner_session_id,
                token=self.token,
                generation=self.generation,
                now=self.clock(),
            )

    async def active_runs(self) -> frozenset[str]:
        return await asyncio.to_thread(_active_runs, self.db)

    async def fenced_runs(self, run_ids: frozenset[str]) -> frozenset[str]:
        return await asyncio.to_thread(_fenced_runs, self.db, run_ids)

    async def file_regression(
        self, candidate_sha: str, base_sha: str, phase: Phase, diagnostic: str
    ) -> str:
        return await asyncio.to_thread(
            _hub_regression_task,
            self.db,
            self.project_id,
            self.owner_session_id,
            candidate_sha,
            base_sha,
            phase,
            diagnostic,
        )
