"""A registered spawn child whose agent workspace is named from its real project."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager


@dataclass(frozen=True)
class WorkspaceSeat:
    db: HubDatabase
    project_id: str
    project_name: str
    session_id: str
    seq_num: int


def register_workspace_seat(
    db: HubDatabase, project: dict[str, Any], machine_id: str
) -> WorkspaceSeat:
    """Register a child session row, which spawn reads to name and grant the seat's folder."""
    session = SessionManager(db).register(
        external_id="workspace-seat",
        machine_id=machine_id,
        source="codex",
        project_id=str(project["id"]),
    )
    assert session.seq_num is not None
    return WorkspaceSeat(
        db=db,
        project_id=str(project["id"]),
        project_name=str(project["name"]),
        session_id=session.id,
        seq_num=session.seq_num,
    )
