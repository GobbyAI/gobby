"""Serialized workspace ownership with a durable, replayable filesystem journal."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import LIVE_SESSION_STATUSES


class WorkspaceOwnershipError(ValueError):
    """A workspace cannot safely change ownership."""


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _finish_rotation(folder: Path, journal: dict[str, Any]) -> None:
    """Replay the atomic live-directory rename after an interrupted activation."""
    archive_name = journal.get("archive")
    if not isinstance(archive_name, str) or not re.fullmatch(r"session-\d+", archive_name):
        raise WorkspaceOwnershipError("Invalid workspace rotation journal")
    current = folder / "current"
    archive = folder / "history" / archive_name
    archive.parent.mkdir(exist_ok=True)
    if archive.is_symlink() or current.is_symlink():
        raise WorkspaceOwnershipError("Workspace rotation paths must not be symbolic links")
    if archive.exists():
        if current.exists() and any(current.iterdir()):
            raise WorkspaceOwnershipError(f"Workspace archive collision: {archive}")
    elif current.exists():
        current.rename(archive)
    else:
        raise WorkspaceOwnershipError("Workspace rotation lost its live directory")
    _sync_directory(archive.parent)
    current.mkdir(exist_ok=True)
    _sync_directory(folder)


def bind_persistent_workspace(
    db: HubDatabase, session_id: str, project_id: str, definition: str, session_seq: int
) -> Path:
    """Claim a stable definition folder, refusing an independently live owner.

    The transaction lock lasts through an enclosing activation transaction. Metadata
    is outside the backed-up tree, and pending rotations survive database rollback.
    """
    from gobby.agents.agent_workspaces import workspace_anchor, workspace_git_dir

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", definition):
        raise WorkspaceOwnershipError("Invalid workspace definition path")
    folder = workspace_anchor(project_id) / definition
    current = folder / "current"
    metadata = workspace_git_dir(project_id) / "workspace-owners" / f"{definition}.json"
    with db.transaction() as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"persistent-workspace:{project_id}:{definition}",),
        )
        live = connection.execute(
            "SELECT s.id FROM sessions s JOIN session_variables v ON v.session_id = s.id "
            "WHERE s.project_id = %s AND s.id <> %s AND s.status = ANY(%s) "
            "AND v.variables ->> '_agent_workspace_dir' IS NOT NULL "
            "AND (v.variables ->> '_agent_type' = %s "
            "OR v.variables ->> '_agent_workspace_dir' = %s) LIMIT 1",
            (project_id, session_id, list(LIVE_SESSION_STATUSES), definition, str(current)),
        ).fetchone()
        if live is not None:
            raise WorkspaceOwnershipError(f"Workspace has a live owner: {live['id']}")
        state = json.loads(metadata.read_text()) if metadata.exists() else {}
        if folder.is_symlink() or (folder / "history").is_symlink() or current.is_symlink():
            raise WorkspaceOwnershipError("Workspace ownership paths must not be symbolic links")
        folder.mkdir(parents=True, exist_ok=True)
        pending = state.get("pending")
        if pending:
            _finish_rotation(folder, pending)
            state = {"owner": pending["owner"], "seq": pending["seq"]}
            _save(metadata, state)
        # Old layouts have one folder per session. Move whole folders, never merge
        # their contents. A completed rename is also its own migration receipt.
        for legacy in sorted(folder.glob("session-*")):
            if not re.fullmatch(r"session-\d+", legacy.name) or not legacy.is_dir():
                continue
            if legacy.is_symlink():
                raise WorkspaceOwnershipError("Legacy workspace must not be a symbolic link")
            archive = folder / "history" / legacy.name
            archive.parent.mkdir(exist_ok=True)
            if archive.exists() or archive.is_symlink():
                raise WorkspaceOwnershipError(f"Workspace archive collision: {archive}")
            legacy.rename(archive)
            _sync_directory(archive.parent)
            _sync_directory(folder)
        if state.get("owner") == session_id:
            current.mkdir(exist_ok=True)
            return current
        if current.exists() and not state.get("owner") and any(current.iterdir()):
            raise WorkspaceOwnershipError("Workspace has live files without recorded ownership")
        if state.get("owner") and current.exists():
            if not isinstance(state.get("seq"), int):
                raise WorkspaceOwnershipError("Workspace owner has no session reference")
            archive_name = f"session-{state['seq']}"
            archive = folder / "history" / archive_name
            if archive.exists() or archive.is_symlink():
                raise WorkspaceOwnershipError(f"Workspace archive collision: {archive}")
            pending = {
                "archive": archive_name,
                "owner": session_id,
                "seq": session_seq,
            }
            _save(metadata, {**state, "pending": pending})
            _finish_rotation(folder, pending)
        current.mkdir(exist_ok=True)
        _save(metadata, {"owner": session_id, "seq": session_seq})
    return current
