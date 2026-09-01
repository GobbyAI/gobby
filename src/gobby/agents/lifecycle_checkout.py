"""Checkout root resolution for agent lifecycle monitoring."""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
from gobby.storage.workspace_machine_scope import require_local_machine_id


def resolve_session_checkout_root(
    db: HubDatabase,
    session: Any,
) -> str | None:
    """Return the session-machine checkout root, or None when it is missing."""
    project_id = getattr(session, "project_id", None)
    if not project_id:
        return None
    machine_id = getattr(session, "machine_id", None)
    try:
        resolved_machine = require_local_machine_id(
            machine_id, resource_kind="project_checkout", resource_id=str(project_id)
        )
        return require_root(db, str(project_id), resolved_machine)
    except (CheckoutNotFoundError, ValueError, RuntimeError):
        return None
