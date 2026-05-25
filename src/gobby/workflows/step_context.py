"""Helpers for inspecting active step-workflow state."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import pydantic

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.state_manager import WorkflowInstanceManager

logger = logging.getLogger(__name__)


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
    """Return the first active step-workflow context using synchronous DB reads."""
    if not session_id:
        return None
    return _get_active_step_workflow_context(db, session_id)


def _get_active_step_workflow_context(
    db: HubDatabase,
    session_id: str,
) -> StepWorkflowContext | None:
    """Read step workflow state, ignoring malformed workflow definitions."""

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
        except json.JSONDecodeError as exc:
            logger.warning(
                "Skipping malformed step workflow definition %s: invalid JSON: %s",
                instance.workflow_name,
                exc,
                extra=_definition_log_extra(instance, row, session_id),
            )
            continue
        except (TypeError, pydantic.ValidationError) as exc:
            logger.warning(
                "Skipping malformed step workflow definition %s: validation failed: %s",
                instance.workflow_name,
                exc,
                extra=_definition_log_extra(instance, row, session_id),
            )
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


def _definition_log_extra(instance: Any, definition_row: Any, session_id: str) -> dict[str, Any]:
    """Build structured context for malformed step workflow definition logs."""
    row_context = (
        definition_row.to_dict()
        if hasattr(definition_row, "to_dict")
        else {
            "id": getattr(definition_row, "id", None),
            "name": getattr(definition_row, "name", instance.workflow_name),
            "workflow_type": getattr(definition_row, "workflow_type", None),
        }
    )
    return {
        "session_id": session_id,
        "workflow_instance_id": getattr(instance, "id", None),
        "workflow_name": instance.workflow_name,
        "current_step": instance.current_step,
        "workflow_definition_id": getattr(definition_row, "id", None),
        "workflow_definition_json": getattr(definition_row, "definition_json", None),
        "workflow_definition_row": row_context,
    }


def has_active_step_workflow(db: HubDatabase, session_id: str | None) -> bool:
    """Return whether the session has an active step workflow via synchronous DB reads."""
    return get_active_step_workflow_context(db, session_id) is not None
