"""Machine-ownership guards for session-local resources."""

from __future__ import annotations

from typing import Protocol

from gobby.utils.machine_id import get_machine_id


class SessionOwnership(Protocol):
    id: str
    machine_id: str


class RemoteSessionOwnershipError(PermissionError):
    """Raised when this daemon would access another machine's session resources."""


def is_local_machine_owner(
    owner_machine_id: str | None,
    local_machine_id: str | None,
) -> bool:
    """Return whether a resource owner exactly matches the local machine."""
    return bool(owner_machine_id and local_machine_id and owner_machine_id == local_machine_id)


def require_local_session_ownership(session: SessionOwnership) -> str:
    """Require the session's local-resource owner to match this daemon."""
    local_machine_id = get_machine_id()
    owner_machine_id = session.machine_id
    if is_local_machine_owner(owner_machine_id, local_machine_id):
        assert local_machine_id is not None
        return local_machine_id

    owner = owner_machine_id or "unassigned"
    local = local_machine_id or "unavailable"
    raise RemoteSessionOwnershipError(
        f"Session {session.id} belongs to remote machine {owner}; local machine is {local}"
    )
