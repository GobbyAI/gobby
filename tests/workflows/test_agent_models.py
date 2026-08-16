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
    assert "steps" not in fields
    assert "step_variables" not in fields
    assert "exit_condition" not in fields

    stepful = AgentDefinitionBody.model_validate(_load(_STEPFUL_YAML))
    assert stepful.step_workflow is not None
    assert [step.name for step in stepful.step_workflow.steps] == ["plan", "terminate"]
    assert stepful.step_workflow.variables["required_skills"] == ["plan-draft"]
    assert stepful.step_workflow.exit_condition == "current_step == 'terminate'"
    assert stepful.step_workflow.get_step("plan") is not None
    assert stepful.step_workflow.get_step("missing") is None

    restored = AgentDefinitionBody.model_validate(stepful.model_dump())
    assert restored.step_workflow is not None
    assert restored.step_workflow.model_dump() == stepful.step_workflow.model_dump()

    stepless = AgentDefinitionBody.model_validate(_load(_STEPLESS_YAML))
    assert stepless.step_workflow is None
    stepless_restored = AgentDefinitionBody.model_validate(stepless.model_dump())
    assert stepless_restored.step_workflow is None

    with pytest.raises(ValidationError):
        AgentStepWorkflowBody(steps=[])


def test_legacy_step_keys_rejected() -> None:
    """Top-level step fields are gone and fail loud with nested replacement names."""
    from gobby.workflows.agent_models import AgentDefinitionBody

    fields = AgentDefinitionBody.model_fields
    assert "step_workflow" in fields
    assert "steps" not in fields
    assert "step_variables" not in fields
    assert "exit_condition" not in fields

    with pytest.raises(ValidationError, match="step_workflow.steps"):
        AgentDefinitionBody.model_validate(
            {
                "name": "planner",
                "steps": [{"name": "plan", "allowed_tools": "all"}],
            }
        )
    with pytest.raises(ValidationError, match="step_workflow.variables"):
        AgentDefinitionBody.model_validate(
            {
                "name": "planner",
                "step_variables": {"required_skills": ["plan-draft"]},
            }
        )
    with pytest.raises(ValidationError, match="step_workflow.exit_condition"):
        AgentDefinitionBody.model_validate(
            {
                "name": "planner",
                "exit_condition": "current_step == 'terminate'",
            }
        )
