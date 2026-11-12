"""Session workspace bindings and definition activation policy."""

import errno
from pathlib import Path
from typing import Any

from gobby.agents.agent_workspaces import (
    WORKSPACE_DEFINITION_VARIABLE,
    WORKSPACE_DIR_VARIABLE,
    WORKSPACE_EPIC_DIR_VARIABLE,
    WORKSPACE_PERSISTENT_VARIABLE,
    session_workspace_dir,
    workspace_anchor,
)
from gobby.agents.persistent_workspaces import bind_persistent_workspace
from gobby.storage.hub.protocol import HubDatabase

_SESSION_LOCATION_SQL = "SELECT project_id, seq_num FROM sessions WHERE id = %s"


def cleanup_default_workspaces(db: HubDatabase, project_id: str) -> None:
    """Retire default bindings and empty generated folders, preserving every file."""
    from gobby.storage.hub.protocol import SessionVariableMutation
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db)
    rows = db.execute(
        "SELECT s.id FROM sessions s JOIN session_variables v ON v.session_id = s.id "
        "WHERE s.project_id = %s AND coalesce(nullif(v.variables ->> '_agent_type', ''), "
        "v.variables ->> '_agent_workspace_definition', 'default') = 'default' "
        "AND (v.variables ->> '_agent_workspace_dir' IS NOT NULL "
        "OR v.variables ->> '_agent_workspace_epic_dir' IS NOT NULL "
        "OR v.variables ->> '_agent_workspace_persistent' = 'true' "
        "OR v.variables ->> '_agent_workspace_injected' = 'true') "
        "ORDER BY s.id",
        (project_id,),
    ).fetchall()
    for row in rows:
        with db.transaction_immediate(SessionVariableMutation(session_id=row["id"])):
            current = variables.get_variables(row["id"])
            definition = current.get("_agent_type") or current.get(WORKSPACE_DEFINITION_VARIABLE)
            if definition not in (None, "", "default"):
                continue
            variables.merge_variables(
                row["id"],
                {
                    WORKSPACE_DIR_VARIABLE: None,
                    WORKSPACE_EPIC_DIR_VARIABLE: None,
                    WORKSPACE_PERSISTENT_VARIABLE: False,
                    WORKSPACE_DEFINITION_VARIABLE: None,
                    "_agent_workspace_injected": False,
                },
            )
    root = workspace_anchor(project_id) / "default"
    if not root.is_dir() or root.is_symlink():
        return
    try:
        generated = [
            folder
            for folder in root.iterdir()
            if folder.name.startswith("session-")
            and folder.name[8:].isdigit()
            and folder.is_dir()
            and not folder.is_symlink()
        ]
    except FileNotFoundError:
        return
    for directory in [*generated, root]:
        try:
            directory.rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                raise


def _definition_persistent(db: HubDatabase, definition: str | None, project_id: str) -> bool:
    if definition is None:
        return False
    from gobby.storage.definitions import AgentDefinitionManager

    row = AgentDefinitionManager(db).get_by_name(definition, project_id)
    return row is not None and row.definition_json.get("persistent_workspace") is True


def definition_workspace_changes(
    db: HubDatabase, session_id: str, definition: str, *, persistent: bool
) -> dict[str, Any]:
    """Prepare the binding delta for merging under the session identity lock.

    Historical files remain in their old folder; only the current binding and
    injection state change.
    """
    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    folder = None
    if (
        definition != "default"
        and location is not None
        and location["project_id"] is not None
        and location["seq_num"] is not None
    ):
        folder = (
            bind_persistent_workspace(
                db, session_id, location["project_id"], definition, location["seq_num"]
            )
            if persistent
            else session_workspace_dir(location["project_id"], definition, location["seq_num"])
        )
        folder.mkdir(parents=True, exist_ok=True)
    return {
        WORKSPACE_DIR_VARIABLE: str(folder) if folder is not None else None,
        WORKSPACE_EPIC_DIR_VARIABLE: None,
        WORKSPACE_PERSISTENT_VARIABLE: persistent and definition != "default",
        WORKSPACE_DEFINITION_VARIABLE: definition if folder is not None else None,
        "_agent_workspace_injected": False,
    }


def bind_session_workspace(db: HubDatabase, session_id: str) -> Path | None:
    """Serialize acquisition and binding through the same activation transaction."""
    with db.transaction():
        return _bind_session_workspace(db, session_id)


def _bind_session_workspace(db: HubDatabase, session_id: str) -> Path | None:
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db)
    current = variables.get_variables(session_id)
    definition = current.get("_agent_type") or current.get(WORKSPACE_DEFINITION_VARIABLE)
    if not definition or definition == "default":
        location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
        if location is not None and location["project_id"] is not None:
            cleanup_default_workspaces(db, location["project_id"])
        variables.merge_variables(
            session_id,
            {
                WORKSPACE_DIR_VARIABLE: None,
                WORKSPACE_EPIC_DIR_VARIABLE: None,
                WORKSPACE_PERSISTENT_VARIABLE: False,
                WORKSPACE_DEFINITION_VARIABLE: None,
                "_agent_workspace_injected": False,
            },
        )
        return None
    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    if location is None or location["project_id"] is None or location["seq_num"] is None:
        return None
    definition = current.get("_agent_type") or current.get(WORKSPACE_DEFINITION_VARIABLE)
    definition = definition if isinstance(definition, str) and definition else None
    persistent = _definition_persistent(db, definition, location["project_id"])
    if persistent and definition:
        folder = bind_persistent_workspace(
            db, session_id, location["project_id"], definition, location["seq_num"]
        )
    else:
        bound = current.get(WORKSPACE_DIR_VARIABLE)
        if isinstance(bound, str) and bound:
            Path(bound).mkdir(parents=True, exist_ok=True)
            return Path(bound)
        folder = session_workspace_dir(location["project_id"], definition, location["seq_num"])
    folder.mkdir(parents=True, exist_ok=True)
    variables.merge_variables(
        session_id,
        {
            WORKSPACE_DIR_VARIABLE: str(folder),
            WORKSPACE_PERSISTENT_VARIABLE: persistent,
            WORKSPACE_DEFINITION_VARIABLE: definition,
        },
    )
    return folder
