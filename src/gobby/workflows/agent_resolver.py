import json
import logging

from pydantic import ValidationError

from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

logger = logging.getLogger(__name__)


class AgentResolutionError(Exception):
    """Raised when an agent definition contains an extends cycle, breaches max depth, or a parent is missing."""


def _normalize_provider(cli_source: str) -> str:
    """Resolve 'inherit' to provider. Only claude_sdk variants and antigravity need mapping."""
    if cli_source.startswith("claude_sdk") or cli_source == "antigravity":
        return "claude"
    return cli_source  # claude, gemini, codex, cursor, windsurf, copilot pass through


def _merge_agent_bodies(
    child: AgentDefinitionBody, parent: AgentDefinitionBody
) -> AgentDefinitionBody:
    """Merge an agent body into its parent, modifying the child and returning it.
    Child's explicitly-set fields override parent.
    """
    child_set = child.model_fields_set

    # Dump the parent and child models
    merged_data = parent.model_dump()
    child_data = child.model_dump()

    # Overwrite top level parent fields with child fields that were explicitly set
    for field in child_set:
        if field != "workflows":
            merged_data[field] = child_data[field]

    # Handle workflows specially
    parent_wf = parent.workflows
    child_wf = child.workflows
    child_wf_set = child_wf.model_fields_set

    # Workflows rules: concatenate + deduplicate (preserve order from parent then child)
    merged_rules = list(dict.fromkeys(parent_wf.rules + child_wf.rules))

    # Selectors & format: child wins if explicitly set, else parent's
    rule_selectors = (
        child_wf.rule_selectors if "rule_selectors" in child_wf_set else parent_wf.rule_selectors
    )
    skill_selectors = (
        child_wf.skill_selectors if "skill_selectors" in child_wf_set else parent_wf.skill_selectors
    )
    variable_selectors = (
        child_wf.variable_selectors
        if "variable_selectors" in child_wf_set
        else parent_wf.variable_selectors
    )
    skill_format = (
        child_wf.skill_format if "skill_format" in child_wf_set else parent_wf.skill_format
    )

    # Variables: dict merge (child wins on key conflict)
    merged_vars = {**parent_wf.variables, **child_wf.variables}

    merged_workflows = AgentWorkflows(
        pipeline=child_wf.pipeline if "pipeline" in child_wf_set else parent_wf.pipeline,
        rules=merged_rules,
        rule_selectors=rule_selectors,
        skill_selectors=skill_selectors,
        variable_selectors=variable_selectors,
        skill_format=skill_format,
        variables=merged_vars,
    )

    merged_data["workflows"] = merged_workflows.model_dump()

    # extends is cleared in merged result
    merged_data["extends"] = None

    return AgentDefinitionBody(**merged_data)


def resolve_agent(
    name: str,
    db: DatabaseProtocol,
    cli_source: str | None = None,
    project_id: str | None = None,
) -> AgentDefinitionBody | None:
    """Resolve an agent by name, following extends chain and applying inheritance.

    - Follows `extends` chain up to `MAX_EXTENDS_DEPTH`
    - Detects cycles
    - Merges from root ancestor → leaf (child overrides parent)
    - Resolves 'inherit' provider from `cli_source`
    """
    MAX_EXTENDS_DEPTH = 5
    seen: set[str] = set()

    manager = LocalWorkflowDefinitionManager(db)

    def load_body(agent_name: str) -> AgentDefinitionBody | None:
        row = manager.get_by_name(agent_name, project_id=project_id, include_templates=True)
        if not row:
            return None

        if row.workflow_type != "agent":
            return None

        if not row.definition_json:
            return None

        try:
            data = json.loads(row.definition_json)
            if "name" not in data:
                data["name"] = row.name
            return AgentDefinitionBody(**data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.debug("Failed to parse agent definition for %s: %s", agent_name, e)
            return None

    # Gather chain from leaf up to root
    chain: list[AgentDefinitionBody] = []
    current_name: str | None = name

    while current_name:
        if len(seen) >= MAX_EXTENDS_DEPTH:
            raise AgentResolutionError(
                f"Agent extends depth exceeded maximum of {MAX_EXTENDS_DEPTH} at agent '{current_name}'"
            )

        if current_name in seen:
            raise AgentResolutionError(
                f"Inheritance cycle detected: agent '{name}' contains a cycle involving '{current_name}'."
            )

        seen.add(current_name)

        body = load_body(current_name)
        if not body:
            if not chain:
                # Top level agent not found
                return None
            else:
                raise AgentResolutionError(f"Parent agent '{current_name}' not found for '{name}'.")

        chain.append(body)
        current_name = body.extends

    # Merge down from root to leaf
    merged_body = chain[-1]

    for child_body in reversed(chain[:-1]):
        merged_body = _merge_agent_bodies(child_body, merged_body)

    # Resolve 'inherit' provider
    if merged_body.provider == "inherit":
        if cli_source:
            merged_body.provider = _normalize_provider(cli_source)
        else:
            merged_body.provider = "claude"

    return merged_body
