"""Registered project names and preservation of legacy workspace roots."""

from __future__ import annotations

import json
import re
import subprocess
from uuid import UUID

from gobby.storage.hub.protocol import HubDatabase


def validate_workspace_project_name(name: str) -> str:
    """Accept one unambiguous, portable directory component."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name.lower().endswith(".git"):
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
    """Resolve metadata and migrate whole roots under the existing advisory-lock pattern.

    The receipt moves with the Git directory, distinguishing an interrupted
    rename from an unrelated destination. Contents are never combined. Live
    owners must release their legacy bindings before those paths can move.
    """
    from gobby.agents.agent_workspaces import workspaces_root
    from gobby.agents.persistent_workspaces import _save, _sync_directory
    from gobby.storage.sessions import LIVE_SESSION_STATUSES
    from gobby.workflows.state_manager import SessionVariableManager

    with db.transaction() as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"workspace-project:{project_id}",)
        )
        name = registered_workspace_project_name(db, project_id)
        root = workspaces_root()
        old, target = root / project_id, root / name
        old_git, target_git = root / f"{project_id}.git", root / f"{name}.git"
        paths = (root, old, target, old_git, target_git)
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
        if target.exists() and recorded is None and not (old.exists() or old_git.exists()):
            raise ValueError("Agent workspace migration destination has no ownership receipt")
        if old.exists() or old_git.exists():
            if (target.exists() or target_git.exists()) and recorded != receipt:
                raise ValueError("Agent workspace migration destination collision")
            if old.exists() and target.exists():
                raise ValueError("Agent workspace migration has two anchors")
            if old_git.exists() and target_git.exists():
                raise ValueError("Agent workspace migration has two Git directories")
            live = connection.execute(
                "SELECT s.id FROM sessions s JOIN session_variables v ON v.session_id = s.id "
                "WHERE s.project_id = %s AND s.status = ANY(%s) "
                "AND (v.variables ->> '_agent_workspace_dir' LIKE %s "
                "OR v.variables ->> '_agent_workspace_epic_dir' LIKE %s) LIMIT 1",
                (project_id, list(LIVE_SESSION_STATUSES), f"{old}/%", f"{old}/%"),
            ).fetchone()
            if live is not None:
                raise ValueError(f"Legacy workspace has a live owner: {live['id']}")
            root.mkdir(parents=True, exist_ok=True)
            if not target_git.exists():
                _save(old_git / "workspace-project.json", receipt)
                old_git.rename(target_git)
                _sync_directory(root)
            if old.exists():
                old.rename(target)
                _sync_directory(root)
        if not marker.exists():
            _save(marker, receipt)
        if (target_git / "HEAD").is_file():
            subprocess.run(
                ["git", f"--git-dir={target_git}", "config", "core.worktree", str(target)],
                check=True,
                capture_output=True,
            )
        # Renames survive database rollback; refresh bindings on every replay.
        variables = SessionVariableManager(db)
        rows = connection.execute(
            "SELECT session_id, variables FROM session_variables WHERE session_id IN "
            "(SELECT id FROM sessions WHERE project_id = %s) "
            "AND (variables ->> '_agent_workspace_dir' LIKE %s "
            "OR variables ->> '_agent_workspace_epic_dir' LIKE %s)",
            (project_id, f"{old}/%", f"{old}/%"),
        ).fetchall()
        for binding in rows:
            changes: dict[str, object] = {}
            values = binding["variables"]
            if isinstance(values, str):
                values = json.loads(values)
            for key in ("_agent_workspace_dir", "_agent_workspace_epic_dir"):
                value = values.get(key)
                if isinstance(value, str) and value.startswith(f"{old}/"):
                    changes[key] = f"{target}/{value[len(str(old)) + 1 :]}"
            if changes:
                changes["_agent_workspace_injected"] = False
                variables.merge_variables(binding["session_id"], changes)
        return name
