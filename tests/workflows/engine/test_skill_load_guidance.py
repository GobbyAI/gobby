"""Tests for concrete recovery guidance during skill-loading steps."""

from __future__ import annotations

import pytest

from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.engine.skill_load_guidance import skill_load_block_guidance

pytestmark = pytest.mark.unit


def _skill_step(*, variable_name: str) -> WorkflowStep:
    return WorkflowStep.model_validate(
        {
            "name": f"load_{variable_name}",
            "allowed_mcp_tools": ["gobby-skills:get_skill"],
            "on_mcp_success": [
                {
                    "server": "gobby-skills",
                    "tool": "get_skill",
                    "action": "set_variable",
                    "variable": f"{variable_name}_loaded",
                    "value": (
                        "all(skill in vars.get('loaded_skills', []) "
                        f"for skill in vars.{variable_name})"
                    ),
                }
            ],
        }
    )


def test_guidance_names_unloaded_required_skills() -> None:
    guidance = skill_load_block_guidance(
        _skill_step(variable_name="required_skills"),
        {
            "required_skills": ["development-discipline", "restraint", "tasks"],
            "loaded_skills": ["development-discipline"],
        },
    )

    assert "development-discipline" not in guidance
    assert 'get_skill", {"name":"restraint"}' in guidance
    assert 'get_skill", {"name":"tasks"}' in guidance
    assert "<skill-name>" not in guidance


def test_guidance_names_unloaded_additional_skills() -> None:
    guidance = skill_load_block_guidance(
        _skill_step(variable_name="additional_skills"),
        {
            "additional_skills": ["python", "yaml"],
            "loaded_skills": ["python"],
        },
    )

    assert 'get_skill", {"name":"yaml"}' in guidance
    assert 'get_skill", {"name":"python"}' not in guidance
    assert "<skill-name>" not in guidance


def test_guidance_keeps_explicit_handler_target() -> None:
    step = WorkflowStep.model_validate(
        {
            "name": "load_skill",
            "allowed_mcp_tools": ["gobby-skills:get_skill"],
            "on_mcp_success": [
                {
                    "server": "gobby-skills",
                    "tool": "get_skill",
                    "when": "tool_input.name == 'plan-review'",
                }
            ],
        }
    )

    guidance = skill_load_block_guidance(step)

    assert 'get_skill", {"name":"plan-review"}' in guidance
