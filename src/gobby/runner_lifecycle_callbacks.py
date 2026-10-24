"""The agent lifecycle monitor's periodic reconciliation callback."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.agents.team_teardown import reconcile_team_teardowns
from gobby.runner_lifecycle_reconcile import (
    _reclassify_reconciliation_pending_runs,
    _rotate_due_managed_credentials,
)
from gobby.runner_maintenance.isolation import team_run_terminator, team_tree_deleter

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")


async def reconcile_agent_lifecycle_state(runner: GobbyRunner) -> int:
    # Rotation goes first so a failing step below cannot starve it: a binding left
    # past its hour is revoked at the next restart, and its live run can never
    # handshake again.
    rotated = await _rotate_due_managed_credentials(runner)
    reclassified = await _reclassify_reconciliation_pending_runs(runner)
    return rotated + reclassified + await _tear_down_closed_teams(runner)


async def _tear_down_closed_teams(runner: GobbyRunner) -> int:
    """End closed teams' seats and delete their trees once startup reconciled the runs."""
    init = getattr(runner, "_subsystem_init_task", None)
    if getattr(runner, "agent_runner", None) is None or (init is not None and not init.done()):
        return 0
    try:
        return await reconcile_team_teardowns(
            runner.database,
            terminate=team_run_terminator(runner),
            delete=team_tree_deleter(runner.database, runner.worktree_delete_executor),
        )
    except Exception:
        logger.exception("Closed team teardown pass failed")
        return 0
