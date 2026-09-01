"""Checkout-backed project_path for web-chat sessions."""

from __future__ import annotations

from typing import Any

from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)

CHECKOUT_REQUIRED_CODE = "checkout_required"
CHECKOUT_REQUIRED_MESSAGE = "No checkout for this project on this machine"


class ChatCheckoutRequiredError(Exception):
    """Raised when a web chat targets a real project with no checkout on this machine.

    The stable ``code`` travels on the websocket error frame so the UI can offer
    checkout registration instead of showing a generic failure.
    """

    code = CHECKOUT_REQUIRED_CODE

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(CHECKOUT_REQUIRED_MESSAGE)


def resolve_chat_session_project_path(
    db: Any,
    project_id: str | None,
    machine_id: str | None,
) -> str | None:
    """Return the session-machine checkout root.

    Checkout-free sentinel projects (and a missing project id) resolve to None so
    the caller keeps its existing behavior. A real project with no checkout on
    this machine, or a session bound to another machine, raises
    ``ChatCheckoutRequiredError`` so no subprocess ever starts in the daemon cwd.
    """
    if not project_id or db is None:
        return None
    if project_id in CHECKOUT_FREE_PROJECT_IDS:
        return None
    try:
        resolved_machine = require_local_machine_id(
            machine_id, resource_kind="project_checkout", resource_id=project_id
        )
        return require_root(db, project_id, resolved_machine)
    except (CheckoutNotFoundError, MachineOwnershipMismatchError) as exc:
        raise ChatCheckoutRequiredError(project_id) from exc
