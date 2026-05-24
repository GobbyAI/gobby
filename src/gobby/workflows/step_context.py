"""Helpers for inspecting active step-workflow state."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pydantic

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.state_manager import WorkflowInstanceManager


@dataclass(frozen=True)
class StepWorkflowContext:
    """Current step details for a session-owned step workflow."""

    workflow_name: str
    current_step: str
    description: str | None
    status_message: str | None
    exit_condition: str | None


def get_active_step_workflow_context(
    db: HubDatabase,
    session_id: str | None,
) -> StepWorkflowContext | None:
    """Return the first active step-workflow context for a session."""
    if not session_id:
        return None

    instance_manager = WorkflowInstanceManager(db)
    definition_manager = LocalWorkflowDefinitionManager(db)

    for instance in instance_manager.get_active_instances(session_id):
        if not instance.current_step:
            continue

        row = definition_manager.get_by_name(instance.workflow_name)
        if row is None or row.workflow_type == "pipeline":
            continue

        try:
            definition = WorkflowDefinition(**json.loads(row.definition_json))
        except (json.JSONDecodeError, TypeError, pydantic.ValidationError):
            continue

        step = definition.get_step(instance.current_step)
        if step is None:
            continue

        return StepWorkflowContext(
            workflow_name=instance.workflow_name,
            current_step=instance.current_step,
            description=step.description,
            status_message=step.status_message,
            exit_condition=definition.exit_condition,
        )

    return None


def has_active_step_workflow(db: HubDatabase, session_id: str | None) -> bool:
    """Return whether the session has an active step workflow."""
    return get_active_step_workflow_context(db, session_id) is not None
