"""Recovery guidance for step-gated skill loading."""

from __future__ import annotations

import re

from gobby.skills.formatting import skill_fetch_proxy_path
from gobby.workflows.definitions import WorkflowStep

_SKILL_LOAD_TARGET_PATTERN = re.compile(r"tool_input\.name\s*==\s*['\"]([^'\"]+)['\"]")


def skill_load_block_guidance(step: WorkflowStep) -> str:
    """Return recovery guidance when a skill-loading step blocks a wrong tool."""
    if not _is_skill_load_step(step):
        return ""

    targets = _skill_load_targets(step)
    if targets:
        calls = ", then ".join(skill_fetch_proxy_path(target) for target in targets)
    else:
        calls = (
            'list_mcp_servers -> list_tools("gobby-skills") -> '
            'get_tool_schema("gobby-skills", "get_skill") -> '
            'call_tool("gobby-skills", "get_skill", {"name": "<skill-name>"})'
        )

    return (
        "\nDuring this skill-loading step, call gobby-skills:get_skill through "
        f"mcp__gobby__ progressive discovery: {calls}."
    )


def _is_skill_load_step(step: WorkflowStep) -> bool:
    name_is_skill_step = (
        step.name in {"load_skill", "load_skills", "load_required_skills"}
        or step.name.startswith("load_")
        and "skill" in step.name
    )
    allows_get_skill = (
        step.allowed_mcp_tools == "all" or "gobby-skills:get_skill" in step.allowed_mcp_tools
    )
    return name_is_skill_step and allows_get_skill


def _skill_load_targets(step: WorkflowStep) -> list[str]:
    targets: list[str] = []
    for handler in step.on_mcp_success:
        if handler.get("server") != "gobby-skills" or handler.get("tool") != "get_skill":
            continue
        condition = handler.get("when")
        if not isinstance(condition, str):
            continue
        match = _SKILL_LOAD_TARGET_PATTERN.search(condition)
        if match:
            targets.append(match.group(1))
    return targets
