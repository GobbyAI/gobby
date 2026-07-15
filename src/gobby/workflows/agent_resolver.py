import json
import logging

from pydantic import ValidationError

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody

logger = logging.getLogger(__name__)


class AgentResolutionError(Exception):
    """Raised when an agent definition cannot be found or parsed."""


def resolve_agent(
    name: str,
    db: HubDatabase,
    cli_source: str | None = None,
    project_id: str | None = None,
) -> AgentDefinitionBody | None:
    """Resolve an agent by name via direct DB lookup.

    - Looks up agent by name in workflow_definitions
    - Resolves 'inherit' provider from `cli_source`
    - Returns None if agent not found (except 'default' which returns Pydantic defaults)
    """
    manager = LocalWorkflowDefinitionManager(db)

    row = manager.get_by_name(name, project_id=project_id, workflow_type="agent")
    if not row or row.workflow_type != "agent" or not row.definition_json:
        if name == "default":
            return AgentDefinitionBody(name="default", mode="inherit")
        return None

    try:
        data = json.loads(row.definition_json)
        if "name" not in data:
            data["name"] = row.name
        body = AgentDefinitionBody(**data)
    except (json.JSONDecodeError, TypeError, ValidationError) as e:
        logger.warning("Failed to parse agent definition for %s: %s", name, e, exc_info=True)
        return None

    # Resolve 'inherit' provider from the session source.
    if body.provider == "inherit":
        if cli_source:
            body.provider = cli_source
        else:
            body.provider = "claude"

    return body
