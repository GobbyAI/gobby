import hashlib
import json
import logging

from pydantic import ValidationError

from gobby.storage.definitions.agents import (
    AgentDefinitionManager,
    AgentDefinitionRow,
    reject_terminal_backend,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import AgentDefinitionBody, AgentSelector, AgentWorkflows
from gobby.workflows.state_manager import SessionVariableManager

logger = logging.getLogger(__name__)


def session_agent_definition(
    db: HubDatabase, session: Session
) -> tuple[AgentDefinitionBody, str] | None:
    """Resolve a session's enabled installed definition and its content digest."""
    agent_type = (
        SessionVariableManager(db)
        .get_variable_subset(session.id, ("_agent_type",))
        .get("_agent_type")
    )
    if not isinstance(agent_type, str) or not agent_type or session.project_id is None:
        return None
    row = AgentDefinitionManager(db).get_by_name(agent_type, session.project_id)
    if row is None or not row.enabled:
        return None
    try:
        body = AgentDefinitionBody.model_validate({**row.definition_json, "name": row.name})
    except ValidationError:
        logger.warning("Invalid active agent definition for session %s", session.id)
        return None
    digest = hashlib.sha256(
        json.dumps(row.definition_json, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body, digest


def session_has_capability(db: HubDatabase, session_id: str, capability: str) -> bool:
    """Whether a known session's enabled definition grants this permission."""
    session = SessionManager(db).get(session_id)
    resolved = session_agent_definition(db, session) if session is not None else None
    return resolved is not None and capability in resolved[0].capabilities


def session_has_capability_or_operator(db: HubDatabase, session_id: str, capability: str) -> bool:
    """Recognize a declared permission or the operator's own unspawned terminal."""
    session = SessionManager(db).get(session_id)
    if session is None:
        return False
    role = SessionVariableManager(db).get_variable_subset(session_id, ("_agent_type",))
    if (
        not session.agent_run_id
        and not session.agent_depth
        and role.get("_agent_type")
        in (
            None,
            "default",
        )
    ):
        return True
    resolved = session_agent_definition(db, session)
    return resolved is not None and capability in resolved[0].capabilities


class AgentResolutionError(Exception):
    """Raised when an agent definition cannot be found or parsed."""


def _body_from_row(row: AgentDefinitionRow, name: str) -> AgentDefinitionBody:
    data = row.definition_json
    if isinstance(data, str):
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise TypeError(f"Agent definition '{name}' is not a JSON object")
        data = parsed
    elif not isinstance(data, dict):
        raise TypeError(f"Agent definition '{name}' is not a JSON object")
    payload = dict(data)
    payload.setdefault("name", row.name)
    return AgentDefinitionBody.model_validate(payload)


def _resolve_inherit(body: AgentDefinitionBody, cli_source: str | None) -> AgentDefinitionBody:
    if body.provider == "inherit":
        body.provider = cli_source if cli_source else "claude"
    return body


def resolve_cold_start_command(
    db: HubDatabase, command: str, *, project_id: str | None = None
) -> str | None:
    """Resolve a unique alias from enabled interactive definitions in project scope."""
    matches: list[str] = []
    for row in AgentDefinitionManager(db).list_resolved(project_id=project_id):
        if not row.enabled:
            continue
        try:
            reject_terminal_backend(row.definition_json)
            body = _body_from_row(row, row.name)
        except (ValueError, TypeError):
            continue
        if body.supports_cold_start() and command.casefold() in body.cold_start_commands:
            matches.append(row.name)
    if len(matches) > 1:
        raise AgentResolutionError(f"Cold-start command {command!r} matches multiple definitions")
    return matches[0] if matches else None


def resolve_agent_with_row(
    name: str,
    db: HubDatabase,
    cli_source: str | None = None,
    project_id: str | None = None,
) -> tuple[AgentDefinitionBody, AgentDefinitionRow] | None:
    """Resolve an agent and its hydrated row via the typed manager.

    Raises:
        AgentResolutionError: The stored row names a terminal backend (#23212).
    """
    row = AgentDefinitionManager(db).get_by_name(name, project_id=project_id)
    if row is None:
        return None
    try:
        reject_terminal_backend(row.definition_json)
    except ValueError as e:
        raise AgentResolutionError(str(e)) from e
    try:
        body = _resolve_inherit(_body_from_row(row, name), cli_source)
    except (json.JSONDecodeError, TypeError, ValidationError) as e:
        logger.warning("Failed to parse agent definition for %s: %s", name, e, exc_info=True)
        return None
    return body, row


def resolve_agent(
    name: str,
    db: HubDatabase,
    cli_source: str | None = None,
    project_id: str | None = None,
) -> AgentDefinitionBody | None:
    """Resolve an agent by name via the typed agent manager.

    - Looks up the hydrated agent_definitions row (with optional step_workflow)
    - Resolves 'inherit' provider from `cli_source`
    - Returns None if agent not found (except 'default' which returns Pydantic defaults)
    """
    found = resolve_agent_with_row(name, db, cli_source=cli_source, project_id=project_id)
    if found is not None:
        return found[0]
    if name == "default":
        return _resolve_inherit(
            AgentDefinitionBody(
                name="default",
                workflows=AgentWorkflows(rule_selectors=AgentSelector(include=["tag:default"])),
                prompts={"agent": "Work from the supplied task prompt."},
            ),
            cli_source,
        )
    return None
