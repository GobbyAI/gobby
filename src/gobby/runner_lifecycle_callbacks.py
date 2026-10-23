"""The agent lifecycle monitor's periodic reconciliation callback."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.runner_lifecycle_reconcile import (
    _reclassify_reconciliation_pending_runs,
    _rotate_due_managed_credentials,
)

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


async def reconcile_agent_lifecycle_state(runner: GobbyRunner) -> int:
    # Rotation goes first so a failing step below cannot starve it: a binding left
    # past its hour is revoked at the next restart, and its live run can never
    # handshake again.
    rotated = await _rotate_due_managed_credentials(runner)
    reclassified = await _reclassify_reconciliation_pending_runs(runner)
    return rotated + reclassified
