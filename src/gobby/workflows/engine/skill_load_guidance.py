"""Recovery guidance for step-gated skill loading."""

from __future__ import annotations

import re

from gobby.skills.formatting import skill_fetch_batch_directive, skill_fetch_directive
from gobby.workflows.definitions import WorkflowStep

_SKILL_LOAD_TARGET_PATTERN = re.compile(r"tool_input\.name\s*==\s*['\"]([^'\"]+)['\"]")
_SKILL_LIST_VARIABLE_PATTERN = re.compile(
    r"vars(?:\.([A-Za-z_][A-Za-z0-9_]*)|\.get\(\s*['\"]([^'\"]+)['\"]\s*[,\)])"
)
_SKILL_LIST_VARIABLES = {"required_skills", "additional_skills"}


def skill_load_block_guidance(
    step: WorkflowStep,
    variables: dict[str, object] | None = None,
) -> str:
    """Return recovery guidance when a skill-loading step blocks a wrong tool."""
    if not _is_skill_load_step(step):
        return ""

    targets, declares_targets = _skill_load_targets(step, variables or {})
    if targets:
        directive = skill_fetch_batch_directive(targets)
    elif declares_targets:
        directive = "All skills declared for this step are loaded. Continue after it advances."
    else:
        directive = skill_fetch_directive("<skill-name>")
    return f"\nDuring this skill-loading step:\n{directive}"


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


def _skill_load_targets(
    step: WorkflowStep,
    variables: dict[str, object],
) -> tuple[list[str], bool]:
    targets: list[str] = []
    declares_targets = False
    for handler in step.on_mcp_success:
        if handler.get("server") != "gobby-skills" or handler.get("tool") != "get_skill":
            continue
        condition = handler.get("when")
        if isinstance(condition, str):
            matches = _SKILL_LOAD_TARGET_PATTERN.findall(condition)
            targets.extend(matches)
            declares_targets = declares_targets or bool(matches)

        for expression in (condition, handler.get("value")):
            if not isinstance(expression, str):
                continue
            for match in _SKILL_LIST_VARIABLE_PATTERN.finditer(expression):
                variable_name = match.group(1) or match.group(2)
                if variable_name not in _SKILL_LIST_VARIABLES:
                    continue
                declares_targets = True
                value = variables.get(variable_name)
                if isinstance(value, list):
                    targets.extend(item for item in value if isinstance(item, str))

    loaded = variables.get("loaded_skills")
    loaded_skills = (
        {item for item in loaded if isinstance(item, str)} if isinstance(loaded, list) else set()
    )
    ordered_unloaded = list(
        dict.fromkeys(target for target in targets if target not in loaded_skills)
    )
    return ordered_unloaded, declares_targets
