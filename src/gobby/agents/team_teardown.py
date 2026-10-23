"""Collapse a closed task's team: end its seats, then delete its tree safely (plan 3.3)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from gobby.agents.team_lifecycle import TeamBinding
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.worktrees.deletion import WorktreeDeletionResult

TEARDOWN_KEY = "teardown"
# The closing manager receives its close response before any seat ends.
TEARDOWN_GRACE = timedelta(seconds=30)
FINDING_LABEL = "team-teardown"

TeamRunTerminator = Callable[[AgentRun], Awaitable[bool]]
TeamTreeDeleter = Callable[[TeamBinding], Awaitable[WorktreeDeletionResult]]


async def reconcile_team_teardowns(
    db: HubDatabase,
    *,
    terminate: TeamRunTerminator,
    delete: TeamTreeDeleter,
    now: datetime | None = None,
    grace: timedelta = TEARDOWN_GRACE,
) -> int:
    """One stateless pass over every closed task's team; returns the actions taken."""
    return 0
