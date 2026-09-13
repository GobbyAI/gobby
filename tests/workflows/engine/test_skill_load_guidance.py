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


def test_reference_guidance_uses_exact_completion_ledger() -> None:
    reference = "gobby:references/tasks/closing.md"
    step = _skill_step(variable_name="required_skills")
    variables: dict[str, object] = {
        "required_skills": [reference],
        "loaded_skills": ["gobby", reference],
    }
    assert "references/tasks/closing.md" in skill_load_block_guidance(step, variables)
    variables["loaded_skill_references"] = [reference]
    assert "All skills declared for this step are loaded" in skill_load_block_guidance(
        step, variables
    )


def test_reference_guidance_extracts_explicit_file_target() -> None:
    step = WorkflowStep.model_validate(
        {
            "name": "load_skill",
            "allowed_mcp_tools": ["gobby-skills:get_skill_file"],
            "on_mcp_success": [
                {
                    "server": "gobby-skills",
                    "tool": "get_skill_file",
                    "when": "tool_input.name == 'gobby' and tool_input.path == 'references/tasks/closing.md'",
                }
            ],
        }
    )
    guidance = skill_load_block_guidance(step, {"loaded_skills": ["gobby"]})
    assert '"path":"references/tasks/closing.md"' in guidance
    assert "get_tool_schema" in guidance


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


def test_hook_reference_fetch_injects_directive_without_instruction_body() -> None:
    from gobby.hooks.dispatchers.mcp import format_discovery_result
    from gobby.skills.formatting import skill_fetch_directive

    result = {
        "file": {
            "skill_name": "gobby",
            "path": "references/tasks/closing.md",
            "content": "Private instruction body must be explicitly loaded.",
        },
        "page": {"complete": False, "next_cursor": "opaque"},
    }
    for payload in (result, {"result": result}):
        guidance = format_discovery_result({"tool": "get_skill_file", "result": payload})
        assert guidance == skill_fetch_directive("gobby:references/tasks/closing.md")
        assert "Private instruction body" not in guidance
    for skill_file in ({}, {"skill_name": "gobby", "path": "references/../secret.md"}):
        assert (
            format_discovery_result({"tool": "get_skill_file", "result": {"file": skill_file}})
            == ""
        )
