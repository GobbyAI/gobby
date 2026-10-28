"""Release lease stores: the daemon's fenced view, and the seat's daemon client.

``HubReleaseLease`` runs where the hub database and the release window live. The
daemon's release tools and in-process tests use it. ``DaemonReleaseLease`` runs in the
Release Manager's seat, which holds no database credentials: every release read and
write crosses a daemon call that carries the seat's token and generation, and the
daemon checks both before it answers. Regression filing goes through gobby-tasks as the
Release Manager session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.agents.recovery_state import is_reconciliation_pending
from gobby.releases.batch import Checkout, Phase, ReleaseStoreUnavailable
from gobby.releases.window import (
    ReleaseWindow,
    ReleaseWindowRefused,
    release_window,
    renew_window,
    require_lease,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.project_checkouts import require_root
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

_RUN_PAGE = 500
_TAIL = 2000
_AGENTS = "gobby-agents"
_TASKS = "gobby-tasks"

DaemonCall = Callable[[str, str, str, dict[str, object]], Awaitable[tuple[int, object]]]


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


def _active_runs(db: HubDatabase, machine_id: str) -> frozenset[str]:
    """The machine's active runs that are not already reconciliation-fenced."""
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
            root = await asyncio.to_thread(
                require_root, self.db, self.project_id, require_machine_id()
            )
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
        return await asyncio.to_thread(_active_runs, self.db, require_machine_id())

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


@dataclass
class DaemonReleaseLease:
    """The Release Manager seat's lease. Each call names the seat's session to the daemon.

    A daemon refusal from a release tool raises ``ReleaseWindowRefused``: the lease is
    lost. Any other unanswered or failed call raises ``ReleaseStoreUnavailable``, which
    the batch treats as retryable.
    """

    call: DaemonCall
    owner_session_id: str
    token: str
    generation: int
    # A renewal advances the generation the next call sends; the lock keeps the two in step.
    _fence: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def _answer(self, server: str, tool: str, arguments: dict[str, object]) -> dict[str, Any]:
        status, body = await self.call(self.owner_session_id, server, tool, arguments)
        if status == 200 and isinstance(body, dict) and body.get("success") is True:
            # The daemon wraps a successful tool result under "result"; a refusal stays flat.
            result = body.get("result")
            if isinstance(result, dict):
                return result
            raise ReleaseStoreUnavailable(f"{server}.{tool} answered without a result")
        if server == _AGENTS and status == 200 and isinstance(body, dict):
            code = body.get("error_code")
            if isinstance(code, str):
                raise ReleaseWindowRefused(code, str(body.get("error", code)))
        detail = body.get("error") if isinstance(body, dict) else None
        raise ReleaseStoreUnavailable(
            f"{server}.{tool} did not answer: HTTP {status} {detail or ''}"
        )

    def _window_arguments(self, arguments: dict[str, object]) -> dict[str, object]:
        return {"token": self.token, "generation": self.generation, **arguments}

    async def _window(self, tool: str, arguments: dict[str, object]) -> dict[str, Any]:
        async with self._fence:
            return await self._answer(_AGENTS, tool, self._window_arguments(arguments))

    async def _run_ids(self, tool: str, arguments: dict[str, object]) -> frozenset[str]:
        ids = (await self._window(tool, arguments)).get("run_ids")
        if not isinstance(ids, list):
            raise ReleaseStoreUnavailable(f"{tool} returned no run_ids")
        return frozenset(str(run_id) for run_id in ids)

    async def checkout(self) -> Checkout:
        body = await self._window("release_batch_checkout", {})
        return Checkout(Path(str(body["root"])), str(body["candidate_sha"]))

    async def renew(self) -> None:
        async with self._fence:
            body = await self._answer(_AGENTS, "renew_release_window", self._window_arguments({}))
            self.generation = int(body["window"]["generation"])

    async def release(self) -> None:
        await self._window("end_release_window", {})

    async def active_runs(self) -> frozenset[str]:
        return await self._run_ids("release_batch_active_runs", {})

    async def fenced_runs(self, run_ids: frozenset[str]) -> frozenset[str]:
        return await self._run_ids("release_batch_fenced_runs", {"run_ids": sorted(run_ids)})

    async def file_regression(
        self, candidate_sha: str, base_sha: str, phase: Phase, diagnostic: str
    ) -> str:
        label = _regression_label(candidate_sha)
        listing = await self._answer(
            _TASKS, "list_tasks", {"label": label, "closed": False, "limit": 1}
        )
        if tasks := listing.get("tasks"):
            return str(tasks[0]["id"])
        created = await self._answer(
            _TASKS,
            "create_task",
            {
                "title": f"Release regression in {candidate_sha[:12]}",
                "description": _regression_description(candidate_sha, base_sha, phase, diagnostic),
                "priority": 1,
                "task_type": "bug",
                "labels": [label],
                "category": "code",
                "implementation_domain": "backend",
                "validation_criteria": _regression_criteria(candidate_sha, phase),
            },
        )
        return str(created["id"])
