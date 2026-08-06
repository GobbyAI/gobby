"""Machine-ownership guards shared by local workspace storage managers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id

WorkspaceResourceKind = Literal["worktree", "clone"]

_WORKSPACE_TABLES: dict[WorkspaceResourceKind, str] = {
    "worktree": "worktrees",
    "clone": "clones",
}


class MachineOwnershipMismatchError(RuntimeError):
    """Raised when a daemon-local operation targets a foreign-owned resource."""

    error_code = "machine_ownership_mismatch"

    def __init__(
        self,
        *,
        resource_kind: str,
        resource_id: str,
        owner_machine_id: str,
        current_machine_id: str,
    ) -> None:
        self.resource_kind = resource_kind
        self.resource_id = resource_id
        self.owner_machine_id = owner_machine_id
        self.current_machine_id = current_machine_id
        super().__init__(
            f"{resource_kind.capitalize()} {resource_id} belongs to machine "
            f"{owner_machine_id}, not current machine {current_machine_id}"
        )

    def to_dict(self) -> dict[str, str | bool]:
        """Serialize the stable ownership-failure envelope used by adapters."""
        return {
            "success": False,
            "error": str(self),
            "error_code": self.error_code,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "owner_machine_id": self.owner_machine_id,
            "current_machine_id": self.current_machine_id,
        }


def require_local_machine_id(
    provided_machine_id: str | None,
    *,
    resource_kind: str,
    resource_id: str,
) -> str:
    """Resolve absent ingress identity and reject an explicit foreign identity."""
    current_machine_id = require_machine_id()
    if provided_machine_id is not None and provided_machine_id != current_machine_id:
        raise MachineOwnershipMismatchError(
            resource_kind=resource_kind,
            resource_id=resource_id,
            owner_machine_id=provided_machine_id,
            current_machine_id=current_machine_id,
        )
    return current_machine_id


def get_owned_workspace_row(
    db: HubDatabase,
    resource_kind: WorkspaceResourceKind,
    resource_id: str,
    *,
    current_machine_id: str | None = None,
) -> Mapping[str, Any] | None:
    """Return one local workspace row or raise when its UUID is foreign-owned."""
    machine_id = current_machine_id or require_machine_id()
    table = _WORKSPACE_TABLES[resource_kind]
    row = db.fetchone(
        f"SELECT * FROM {table} WHERE id = %s AND machine_id = %s",  # nosec B608
        (resource_id, machine_id),
    )
    if row is not None:
        return row
    raise_if_foreign_workspace(db, resource_kind, resource_id, current_machine_id=machine_id)
    return None


def raise_if_foreign_workspace(
    db: HubDatabase,
    resource_kind: WorkspaceResourceKind,
    resource_id: str,
    *,
    current_machine_id: str | None = None,
) -> None:
    """Distinguish a missing workspace UUID from a foreign-owned UUID."""
    machine_id = current_machine_id or require_machine_id()
    table = _WORKSPACE_TABLES[resource_kind]
    row = db.fetchone(
        f"SELECT machine_id FROM {table} WHERE id = %s",  # nosec B608
        (resource_id,),
    )
    if row is None:
        return
    owner_machine_id = str(row["machine_id"])
    if owner_machine_id != machine_id:
        raise MachineOwnershipMismatchError(
            resource_kind=resource_kind,
            resource_id=resource_id,
            owner_machine_id=owner_machine_id,
            current_machine_id=machine_id,
        )


def session_is_local(
    db: HubDatabase,
    session_id: str,
    *,
    current_machine_id: str | None = None,
) -> bool:
    """Validate a workspace session binding without mutating either resource."""
    machine_id = current_machine_id or require_machine_id()
    row = db.fetchone("SELECT machine_id FROM sessions WHERE id = %s", (session_id,))
    if row is None:
        return False
    owner_machine_id = str(row["machine_id"])
    if owner_machine_id != machine_id:
        raise MachineOwnershipMismatchError(
            resource_kind="session",
            resource_id=session_id,
            owner_machine_id=owner_machine_id,
            current_machine_id=machine_id,
        )
    return True
