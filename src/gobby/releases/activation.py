"""Daemon activation guard and the project's primary checkout lookup.

The release window and its lease are retired: nothing acquires one, so landings and new
team admission no longer wait on a window. What remains is the guard that keeps a
spawned agent from stopping, starting, restarting or cutting over the daemon, and the
lookup that resolves a project's primary checkout for landing.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from gobby.storage.project_checkouts import require_root
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


def agent_activation_refusal() -> str | None:
    """Refuse a daemon activation step from a spawned agent.

    The operator, persona and Orchestrator panes carry no agent run id and pass. A
    spawned agent is refused, whatever environment it carries.
    """
    if os.environ.get("GOBBY_AGENT_RUN_ID", "").strip():
        return (
            "spawned_agent_refused: spawned agents may not stop, start, restart or cut over "
            "the daemon; the Orchestrator owns activation"
        )
    return None


def project_checkout_root(db: HubDatabase, project_id: str) -> str:
    """The machine's primary checkout root. It blocks on the database: run it on a thread."""
    return require_root(db, project_id, require_machine_id())
