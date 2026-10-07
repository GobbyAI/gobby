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
prompts:
  agent: Run the assigned task.
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
workflows:
  rule_selectors:
    include: []
"""

_STEPLESS_YAML = """
name: coder
description: Step-less agent
prompts:
  agent: Run the assigned task.
workflows:
  rule_selectors:
    include: []
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


@pytest.mark.parametrize("checkout_mode", ["clone", "worktree"])
@pytest.mark.parametrize("include_current_key", [False, True])
def test_removed_isolation_key_rejected(checkout_mode: str, include_current_key: bool) -> None:
    from gobby.workflows.agent_models import AgentDefinitionBody

    data = _load(_STEPLESS_YAML)
    data["isolation"] = checkout_mode
    if include_current_key:
        data["checkout_mode"] = "none"
    with pytest.raises(ValidationError, match="isolation.*use checkout_mode"):
        AgentDefinitionBody.model_validate(data)


@pytest.mark.parametrize("checkout_mode", ["none", "worktree", "clone", "inherit"])
def test_checkout_mode_preserved(checkout_mode: str) -> None:
    from gobby.workflows.agent_models import AgentDefinitionBody

    data = _load(_STEPLESS_YAML)
    data["checkout_mode"] = checkout_mode
    assert AgentDefinitionBody.model_validate(data).checkout_mode == checkout_mode


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
