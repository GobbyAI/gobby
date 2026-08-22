"""Shared constructors for typed agent-step instance tests."""

from __future__ import annotations

from typing import Any

from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.step_instances import AgentStepInstance, build_step_instance


def make_step_instance(
    session_id: str,
    *,
    agent_name: str = "worker",
    current_step: str = "claim",
    variables: dict[str, Any] | None = None,
    steps: list[str] | None = None,
    step_workflow_id: str | None = None,
    status_message: str | None = None,
) -> AgentStepInstance:
    """Build a detached typed instance for fixtures and isolated tests."""
    names = list(steps or (current_step, "implement"))
    if current_step not in names:
        names = [current_step, *names]
    return build_step_instance(
        AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name=agent_name,
            surfaces=["spawn", "persona"],
            step_workflow=AgentStepWorkflowBody(
                variables=dict(variables or {}),
                steps=[
                    WorkflowStep(
                        name=name,
                        status_message=status_message if name == current_step else None,
                    )
                    for name in names
                ],
            ),
        ),
        session_id=session_id,
        step_workflow_id=step_workflow_id,
        current_step=current_step,
        variables=variables,
    )
