"""Team launch inputs a runbook passes at admission, checked against their definitions.

The check is the one review-grant rule every reviewer binding passes at admission and at
spawn (redesign 7.2): the named definition must be enabled and grant the review step.
"""

from __future__ import annotations

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody


def reviewer_refusal(db: HubDatabase, project_id: str | None, reviewer_agent: str) -> str | None:
    """Why ``reviewer_agent`` cannot review a team, or ``None`` when it can."""
    definition = AgentDefinitionManager(db).get_by_name(reviewer_agent, project_id)
    if definition is None or not definition.enabled:
        return f"reviewer_agent {reviewer_agent} is not an enabled definition"
    body = AgentDefinitionBody.model_validate(
        {**definition.definition_json, "name": definition.name}
    )
    if "review" not in body.task_step_writes:
        return f"reviewer_agent {reviewer_agent} lacks the review step grant"
    return None
