"""Agent inline-step workflow registration."""

from __future__ import annotations

import json

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody


def register_agent_step_workflow(
    agent_body: AgentDefinitionBody,
    db: HubDatabase,
) -> str:
    """Create or refresh the generated step workflow for an agent definition."""
    step_workflow_name = f"{agent_body.name}-steps"
    def_manager = LocalWorkflowDefinitionManager(db)

    wf_data = {
        "name": step_workflow_name,
        "description": f"Auto-generated step workflow for {agent_body.name} agent",
        "type": "step",
        "version": "2.0",
        "enabled": False,
        "steps": [step.model_dump() for step in (agent_body.steps or [])],
        "variables": agent_body.step_variables,
        "exit_condition": agent_body.exit_condition,
    }
    definition_json = json.dumps(wf_data)

    existing = def_manager.get_by_name(step_workflow_name)
    if existing:
        def_manager.update(
            existing.id,
            definition_json=definition_json,
            workflow_type="workflow",
            source="agent",
        )
    else:
        def_manager.create(
            name=step_workflow_name,
            definition_json=definition_json,
            workflow_type="workflow",
            enabled=False,
            source="agent",
        )

    return step_workflow_name
