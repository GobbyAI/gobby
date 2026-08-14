"""Agent inline-step workflow registration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import bump_workflow_definitions_revision
from gobby.workflows.definitions import AgentDefinitionBody


def register_agent_step_workflow(
    agent_body: AgentDefinitionBody,
    db: HubDatabase,
) -> str:
    """Create or refresh the generated step workflow for an agent definition."""
    # P3 scaffolding
    step_workflow_name = f"{agent_body.name}-steps"
    nested = agent_body.step_workflow

    wf_data = {
        "name": step_workflow_name,
        "description": f"Auto-generated step workflow for {agent_body.name} agent",
        "type": "step",
        "version": "2.0",
        "enabled": False,
        "steps": [step.model_dump() for step in (nested.steps if nested else [])],
        "variables": dict(nested.variables) if nested else {},
        "exit_condition": nested.exit_condition if nested else None,
    }
    definition_json = json.dumps(wf_data)
    now = datetime.now(UTC).isoformat()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO workflow_definitions (
                id, project_id, name, description, workflow_type,
                version, enabled, priority, sources,
                definition_json, canvas_json, source, tags,
                created_at, updated_at
            ) VALUES (
                %s, NULL, %s, %s, 'workflow', '2.0', FALSE, 100, NULL,
                %s, NULL, 'agent', NULL, %s, %s
            )
            ON CONFLICT(name, project_id, source) DO UPDATE SET
                description = excluded.description,
                workflow_type = excluded.workflow_type,
                version = excluded.version,
                enabled = excluded.enabled,
                priority = excluded.priority,
                sources = excluded.sources,
                definition_json = excluded.definition_json,
                canvas_json = excluded.canvas_json,
                tags = excluded.tags,
                updated_at = excluded.updated_at,
                deleted_at = NULL
            """,
            (
                str(uuid4()),
                step_workflow_name,
                f"Auto-generated step workflow for {agent_body.name} agent",
                definition_json,
                now,
                now,
            ),
        )
    bump_workflow_definitions_revision()

    return step_workflow_name
