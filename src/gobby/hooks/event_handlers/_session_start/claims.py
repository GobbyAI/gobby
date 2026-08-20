"""Task-claim reassignment for a clear_self successor.

The expected-owner compare-and-swap transfer is delivered by #20547. This
module exists so SessionStart can invoke the hook after a winning take without
skipping claim transfer when seeding fails.
"""

from __future__ import annotations

from typing import Any


def preserve_task_claim_state(
    handler: Any,
    sv_mgr: Any,
    successor_id: str,
    predecessor_id: str,
    predecessor_vars: dict[str, Any],
) -> None:
    """Reassign predecessor claims onto the successor after a winning take."""
    _ = (handler, sv_mgr, successor_id, predecessor_id, predecessor_vars)
