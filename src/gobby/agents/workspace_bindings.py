"""Session workspace bindings and definition activation policy."""

from pathlib import Path
from typing import Any

from gobby.agents.agent_workspaces import (
    WORKSPACE_DIR_VARIABLE,
    WORKSPACE_EPIC_DIR_VARIABLE,
    WORKSPACE_PERSISTENT_VARIABLE,
    session_workspace_dir,
)
from gobby.storage.hub.protocol import HubDatabase

_SESSION_LOCATION_SQL = "SELECT project_id, seq_num FROM sessions WHERE id = %s"


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
        folder = session_workspace_dir(location["project_id"], definition, location["seq_num"])
        folder.mkdir(parents=True, exist_ok=True)
    return {
        WORKSPACE_DIR_VARIABLE: str(folder) if folder is not None else None,
        WORKSPACE_EPIC_DIR_VARIABLE: None,
        WORKSPACE_PERSISTENT_VARIABLE: persistent and definition != "default",
        "_agent_workspace_injected": False,
    }


def bind_session_workspace(
    db: HubDatabase, session_id: str, *, inherit_from: str | None = None
) -> Path | None:
    """Bind once, preserving an existing binding across compaction and resume.

    Persistent definitions and sandboxed clear successors inherit the predecessor's
    folder. Other sessions receive their own sequence folder.
    """
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(db)
    current = variables.get_variables(session_id)
    definition = current.get("_agent_type")
    if not definition or definition == "default":
        variables.merge_variables(
            session_id,
            {
                WORKSPACE_DIR_VARIABLE: None,
                WORKSPACE_EPIC_DIR_VARIABLE: None,
                WORKSPACE_PERSISTENT_VARIABLE: False,
                "_agent_workspace_injected": False,
            },
        )
        return None
    bound = current.get(WORKSPACE_DIR_VARIABLE)
    if isinstance(bound, str) and bound:
        Path(bound).mkdir(parents=True, exist_ok=True)
        return Path(bound)
    location = db.execute(_SESSION_LOCATION_SQL, (session_id,)).fetchone()
    if location is None or location["project_id"] is None or location["seq_num"] is None:
        return None
    definition = current.get("_agent_type")
    definition = definition if isinstance(definition, str) and definition else None
    persistent = _definition_persistent(db, definition, location["project_id"])
    folder: Path | None = None
    if inherit_from is not None:
        predecessor = db.execute(
            "SELECT sandbox_enabled FROM sessions WHERE id = %s", (inherit_from,)
        ).fetchone()
        if persistent or (predecessor is not None and predecessor["sandbox_enabled"]):
            inherited = variables.get_variable_subset(inherit_from, [WORKSPACE_DIR_VARIABLE]).get(
                WORKSPACE_DIR_VARIABLE
            )
            if isinstance(inherited, str) and inherited:
                folder = Path(inherited)
    if folder is None:
        folder = session_workspace_dir(location["project_id"], definition, location["seq_num"])
    folder.mkdir(parents=True, exist_ok=True)
    variables.merge_variables(
        session_id,
        {WORKSPACE_DIR_VARIABLE: str(folder), WORKSPACE_PERSISTENT_VARIABLE: persistent},
    )
    return folder
