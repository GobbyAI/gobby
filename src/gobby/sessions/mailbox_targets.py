"""Resolve explicit machine-local and project-local mailbox broadcasts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from gobby.storage.sessions import (
    LIVE_SESSION_STATUS_ORDER,
    SYSTEM_SESSION_SOURCE,
    system_session_id,
)
from gobby.utils.machine_id import require_machine_id


class _Database(Protocol):
    def fetchall(
        self,
        query: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Mapping[str, Any]]: ...


class _SessionStore(Protocol):
    def get(self, session_id: str) -> Any | None: ...


@dataclass(frozen=True)
class BroadcastSelection:
    """Ordered recipient IDs and the scope snapshot used to select them."""

    recipient_session_ids: list[str]
    selector_metadata: dict[str, Any]


def resolve_broadcast_selection(
    *,
    db: _Database,
    session_store: _SessionStore,
    target: str,
    from_session_id: str,
    project_id: str | None,
    resolve_project_ref: Callable[[str], str],
) -> BroadcastSelection:
    """Resolve ``global`` or ``project`` against one machine-owned live population."""
    if target not in {"global", "project"}:
        raise ValueError(f"Unsupported broadcast target: {target}")

    system_sender = from_session_id == system_session_id()
    sender = None if system_sender else session_store.get(from_session_id)
    if not system_sender and sender is None:
        raise ValueError(f"Sender session not found: {from_session_id}")

    sender_machine_id = (
        require_machine_id()
        if sender is None
        else getattr(sender, "machine_id", None) or require_machine_id()
    )
    resolved_project_id: str | None = None
    if target == "global":
        if project_id is not None:
            raise ValueError("project_id is not allowed when target='global'")
    elif system_sender:
        if project_id is None:
            raise ValueError("project_id is required for system-originated project broadcasts")
        resolved_project_id = resolve_project_ref(project_id)
    else:
        if project_id is not None:
            raise ValueError("project_id is not allowed for session-originated project broadcasts")
        resolved_project_id = getattr(sender, "project_id", None)
        if not resolved_project_id:
            raise ValueError(f"Sender session has no project: {from_session_id}")

    status_placeholders = ",".join("%s" for _ in LIVE_SESSION_STATUS_ORDER)
    project_clause = "" if resolved_project_id is None else "AND project_id = %s"
    params: tuple[Any, ...] = (
        *LIVE_SESSION_STATUS_ORDER,
        sender_machine_id,
        SYSTEM_SESSION_SOURCE,
        from_session_id,
    )
    if resolved_project_id is not None:
        params = (*params, resolved_project_id)
    rows = db.fetchall(
        f"""
        SELECT id, status
          FROM sessions
         WHERE status IN ({status_placeholders})
           AND machine_id = %s
           AND source != %s
           AND id != %s
           {project_clause}
         ORDER BY created_at ASC, id ASC
        """,  # nosec B608 -- placeholders and fixed clause are generated locally.
        params,
    )
    recipient_states = [
        {"session_id": str(row["id"]), "status": str(row["status"])} for row in rows
    ]
    return BroadcastSelection(
        recipient_session_ids=[item["session_id"] for item in recipient_states],
        selector_metadata={
            "scope": {
                "kind": target,
                "machine_id": sender_machine_id,
                "project_id": resolved_project_id,
            },
            "recipient_states": recipient_states,
        },
    )
