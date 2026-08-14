"""Model-split coverage for nested AgentStepWorkflowBody."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_STEPFUL_YAML = """
name: planner
description: Nested stepful agent
steps:
  - name: plan
    description: Draft the plan
    allowed_tools: all
step_variables:
  required_skills:
    - plan-draft
exit_condition: "current_step == 'terminate'"
step_workflow:
  variables:
    required_skills:
      - plan-draft
  exit_condition: "current_step == 'terminate'"
  steps:
    - name: plan
      description: Draft the plan
      allowed_tools: all
    - name: terminate
      allowed_tools: []
"""

_STEPLESS_YAML = """
name: coder
description: Step-less agent
"""


def _load(raw: str) -> dict[str, Any]:
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict)
    return loaded


def test_step_workflow_nesting() -> None:
    """Nested step_workflow round-trips for stepful and step-less YAML."""
    from gobby.workflows.agent_models import (
        AgentDefinitionBody,
        AgentStepWorkflowBody,
    )
    from gobby.workflows.definitions import (
        AgentDefinitionBody as ReexportedAgentBody,
    )
    from gobby.workflows.definitions import (
        AgentStepWorkflowBody as ReexportedStepBody,
    )
    from gobby.workflows.definitions import PipelineDefinition
    from gobby.workflows.pipeline_models import PipelineDefinition as PipelineFromModule

    assert AgentStepWorkflowBody is ReexportedStepBody
    assert AgentDefinitionBody is ReexportedAgentBody
    assert PipelineDefinition is PipelineFromModule

    fields = AgentDefinitionBody.model_fields
    assert "step_workflow" in fields
    assert "steps" in fields
    assert "step_variables" in fields
    assert "exit_condition" in fields

    stepful = AgentDefinitionBody.model_validate(_load(_STEPFUL_YAML))
    assert stepful.steps is not None
    assert [step.name for step in stepful.steps] == ["plan"]
    assert stepful.step_variables["required_skills"] == ["plan-draft"]
    assert stepful.exit_condition == "current_step == 'terminate'"
    assert stepful.step_workflow is not None
    assert [step.name for step in stepful.step_workflow.steps] == ["plan", "terminate"]
    assert stepful.step_workflow.variables["required_skills"] == ["plan-draft"]
    assert stepful.step_workflow.exit_condition == "current_step == 'terminate'"
    assert stepful.step_workflow.get_step("plan") is not None
    assert stepful.step_workflow.get_step("missing") is None

    restored = AgentDefinitionBody.model_validate(stepful.model_dump())
    assert restored.step_workflow is not None
    assert restored.step_workflow.model_dump() == stepful.step_workflow.model_dump()
    assert restored.steps is not None
    assert [step.name for step in restored.steps] == ["plan"]

    stepless = AgentDefinitionBody.model_validate(_load(_STEPLESS_YAML))
    assert stepless.step_workflow is None
    assert stepless.steps is None
    assert stepless.step_variables == {}
    assert stepless.exit_condition is None
    stepless_restored = AgentDefinitionBody.model_validate(stepless.model_dump())
    assert stepless_restored.step_workflow is None

    with pytest.raises(ValidationError):
        AgentStepWorkflowBody(steps=[])
