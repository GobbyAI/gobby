"""Session workspace bindings and definition activation policy."""

from pathlib import Path
from typing import Any

from gobby.agents.agent_workspaces import (
    WORKSPACE_DEFINITION_VARIABLE,
    WORKSPACE_DIR_VARIABLE,
    WORKSPACE_EPIC_DIR_VARIABLE,
    WORKSPACE_PERSISTENT_VARIABLE,
    session_workspace_dir,
    workspaces_root,
)
from gobby.agents.persistent_workspaces import bind_persistent_workspace
from gobby.agents.workspace_projects import workspace_project_name
from gobby.storage.hub.protocol import HubDatabase

_SESSION_LOCATION_SQL = "SELECT project_id, seq_num FROM sessions WHERE id = %s"


def _prepare_folder(folder: Path) -> None:
    """Refuse aliases before creating a session directory or its ancestors."""
    root = workspaces_root()
    folder.relative_to(root)
    for candidate in (folder, *folder.parents):
        if candidate.is_symlink():
            raise ValueError("Agent workspace paths must not be symbolic links")
        if candidate == root:
            break
    folder.mkdir(parents=True, exist_ok=True)


def _definition_persistent(db: HubDatabase, definition: str | None, project_id: str) -> bool:
    from gobby.storage.definitions import AgentDefinitionManager

    if not definition or definition == "default":
        return False
    row = AgentDefinitionManager(db).get_by_name(definition, project_id)
    return row is not None and row.definition_json.get("persistent_workspace") is True


def definition_workspace_changes(
    db: HubDatabase, session_id: str, definition: str, *, persistent: bool
) -> dict[str, Any]:
    """Derive workspace authority from the current session, preserving historical files."""
    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    folder = None
    persistent = persistent and definition != "default"
    if (
        location is not None
        and location["project_id"] is not None
        and location["seq_num"] is not None
    ):
        folder = (
            bind_persistent_workspace(
                db, session_id, location["project_id"], definition, location["seq_num"]
            )
            if persistent
            else session_workspace_dir(
                workspace_project_name(db, location["project_id"]), definition, location["seq_num"]
            )
        )
        _prepare_folder(folder)
    return {
        WORKSPACE_DIR_VARIABLE: str(folder) if folder is not None else None,
        WORKSPACE_EPIC_DIR_VARIABLE: None,
        WORKSPACE_PERSISTENT_VARIABLE: persistent,
        WORKSPACE_DEFINITION_VARIABLE: definition if folder is not None else None,
        "_agent_workspace_injected": False,
    }


def bind_session_workspace(db: HubDatabase, session_id: str) -> Path | None:
    """Bind before writable tools; compact and resume retain the session's directory."""
    from gobby.workflows.state_manager import SessionVariableManager

    with db.transaction():
        variables = SessionVariableManager(db)
        current = variables.get_variables(session_id)
        definition = current.get("_agent_type") or current.get(WORKSPACE_DEFINITION_VARIABLE)
        definition = definition if isinstance(definition, str) and definition else "default"
        location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
        if location is None or location["project_id"] is None or location["seq_num"] is None:
            return None
        persistent = _definition_persistent(db, definition, location["project_id"])
        folder = (
            bind_persistent_workspace(
                db, session_id, location["project_id"], definition, location["seq_num"]
            )
            if persistent
            else session_workspace_dir(
                workspace_project_name(db, location["project_id"]), definition, location["seq_num"]
            )
        )
        _prepare_folder(folder)
        changes: dict[str, Any] = {
            WORKSPACE_DIR_VARIABLE: str(folder),
            WORKSPACE_PERSISTENT_VARIABLE: persistent,
            WORKSPACE_DEFINITION_VARIABLE: definition,
        }
        if current.get(WORKSPACE_DIR_VARIABLE) != str(folder):
            changes["_agent_workspace_injected"] = False
        if definition == "default":
            changes[WORKSPACE_EPIC_DIR_VARIABLE] = None
        variables.merge_variables(session_id, changes)
        return folder
