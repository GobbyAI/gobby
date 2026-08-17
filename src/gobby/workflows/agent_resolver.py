import json
import logging

from pydantic import ValidationError

from gobby.storage.definitions.agents import AgentDefinitionManager, AgentDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody

logger = logging.getLogger(__name__)


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


def resolve_agent_with_row(
    name: str,
    db: HubDatabase,
    cli_source: str | None = None,
    project_id: str | None = None,
) -> tuple[AgentDefinitionBody, AgentDefinitionRow] | None:
    """Resolve an agent and its hydrated row via the typed manager."""
    row = AgentDefinitionManager(db).get_by_name(name, project_id=project_id)
    if row is None:
        return None
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
        return _resolve_inherit(AgentDefinitionBody(name="default"), cli_source)
    return None
