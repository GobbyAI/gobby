"""Checkout-backed project_path for web-chat sessions."""

from __future__ import annotations

from typing import Any

from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
from gobby.storage.workspace_machine_scope import require_local_machine_id


def resolve_chat_session_project_path(
    db: Any,
    project_id: str | None,
    machine_id: str | None,
) -> str | None:
    """Return the session-machine checkout root, or None when it is missing."""
    if not project_id or db is None:
        return None
    try:
        resolved_machine = require_local_machine_id(
            machine_id, resource_kind="project_checkout", resource_id=project_id
        )
        return require_root(db, project_id, resolved_machine)
    except (CheckoutNotFoundError, ValueError, RuntimeError):
        return None
