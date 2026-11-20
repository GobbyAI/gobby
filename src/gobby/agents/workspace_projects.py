"""Registered project names that anchor agent workspaces, and their ownership receipts."""

from __future__ import annotations

import json
import re
from uuid import UUID

from gobby.storage.hub.protocol import HubDatabase


def validate_workspace_project_name(name: str) -> str:
    """Accept one unambiguous, portable directory component, system projects included."""
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) or name.lower().endswith(".git"):
        raise ValueError("Invalid project name for agent workspaces")
    try:
        UUID(name)
    except ValueError:
        return name
    raise ValueError("Invalid project name for agent workspaces")


def registered_workspace_project_name(db: HubDatabase, project_id: str) -> str:
    """Resolve an unambiguous project directory name without touching local files."""
    row = db.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()
    if row is None:
        raise ValueError("Agent workspace project is not registered")
    name = validate_workspace_project_name(row["name"])
    collision = db.execute(
        "SELECT id FROM projects WHERE lower(name) = lower(%s) AND id <> %s LIMIT 1",
        (name, project_id),
    ).fetchone()
    if collision is not None:
        raise ValueError("Ambiguous agent workspace project name")
    return name


def workspace_project_name(db: HubDatabase, project_id: str) -> str:
    """Resolve the registered name and hold this machine's ownership receipt for its folder.

    The receipt lives in the project's Git directory and names the project id, so a
    folder left by another project of the same name is refused, never adopted.
    """
    from gobby.agents.agent_workspaces import workspaces_root
    from gobby.agents.persistent_workspaces import _save

    with db.transaction() as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"workspace-project:{project_id}",)
        )
        name = registered_workspace_project_name(db, project_id)
        root = workspaces_root()
        target, target_git = root / name, root / f"{name}.git"
        paths = (root, target, target_git)
        if any(path.is_symlink() for path in paths):
            raise ValueError("Agent workspace project paths must not be symbolic links")
        if any(path.exists() and not path.is_dir() for path in paths):
            raise ValueError("Agent workspace project path is not a directory")
        receipt = {"project_id": project_id, "name": name}
        marker = target_git / "workspace-project.json"
        if marker.is_symlink():
            raise ValueError("Agent workspace project receipt must not be a symbolic link")
        recorded = json.loads(marker.read_text()) if marker.exists() else None
        if recorded is not None and recorded != receipt:
            raise ValueError("Agent workspace destination belongs to another project")
        if target.exists() and recorded is None:
            raise ValueError("Agent workspace destination has no ownership receipt")
        if recorded is None:
            _save(marker, receipt)
        return name
