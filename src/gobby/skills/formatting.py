"""Skill formatting helpers.

Functions for rendering skill lists and fetch directives.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from gobby.skills.metadata import get_skill_category, get_skill_tags

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

SKILL_FETCH_SCHEMA_PATH = (
    'list_mcp_servers -> list_tools("gobby-skills") -> get_tool_schema("gobby-skills", "get_skill")'
)

SKILL_FETCH_CALL_TEMPLATE = 'call_tool("gobby-skills", "get_skill", {{"name": {name_json}}})'

SKILL_FETCH_PROXY_PATH_TEMPLATE = SKILL_FETCH_SCHEMA_PATH + " -> " + SKILL_FETCH_CALL_TEMPLATE


def skill_fetch_proxy_path(name: str) -> str:
    """Return the progressive-discovery proxy path for fetching a skill."""
    return SKILL_FETCH_PROXY_PATH_TEMPLATE.format(name_json=json.dumps(name))


def skill_fetch_call_path(name: str) -> str:
    """Return the get_skill call path after discovery has already completed."""
    return SKILL_FETCH_CALL_TEMPLATE.format(name_json=json.dumps(name))


def skill_fetch_directive(name: str) -> str:
    """Return the canonical agent-facing directive for loading a skill."""
    name_json = json.dumps(name)
    return (
        f"Call get_skill(name={name_json}) on gobby-skills through "
        f"mcp__gobby__ progressive discovery: {skill_fetch_proxy_path(name)}. "
        "Then continue."
    )


def skill_fetch_batch_directive(names: Sequence[str]) -> str:
    """Return a compact directive for loading multiple skills with one discovery pass."""
    skills = _unique_names(names)
    if not skills:
        return ""
    if len(skills) == 1:
        return skill_fetch_directive(skills[0])

    calls = "\n".join(f"- {skill_fetch_call_path(skill)}" for skill in skills)
    return (
        "Use progressive discovery once for gobby-skills get_skill: "
        f"{SKILL_FETCH_SCHEMA_PATH}. Then load these skills in order with:\n"
        f"{calls}\n"
        "Then continue."
    )


def format_skill_fetch_context(name: str, args: str | None = None) -> str:
    """Return a skill fetch directive with optional user arguments preserved."""
    parts = [skill_fetch_directive(name)]
    if args:
        parts.append(f"User arguments: {args}")
    return "\n\n".join(parts)


def _unique_names(names: Sequence[str]) -> list[str]:
    unique: list[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return unique


class SkillLike(Protocol):
    """Protocol for objects that look like a Skill."""

    name: str
    description: str
    enabled: bool
    version: str | None
    metadata: dict[str, Any] | None


def format_skills_json(skills_list: Sequence[SkillLike]) -> str:
    """Format a skills list as a JSON string."""
    output = []
    for skill in skills_list:
        item = {
            "name": skill.name,
            "description": skill.description,
            "enabled": skill.enabled,
            "version": skill.version,
            "category": get_skill_category(skill),
            "tags": get_skill_tags(skill),
        }
        output.append(item)
    return json.dumps(output, indent=2)


def format_skills_markdown_table(skills_list: list[Any]) -> str:
    """Format a skills list as a markdown table."""
    lines = [
        "# Installed Skills",
        "",
        "| Name | Description | Category | Enabled |",
        "|------|-------------|----------|---------|",
    ]

    for skill in skills_list:
        category = (get_skill_category(skill) or "-").replace("|", "\\|")
        enabled = "\u2713" if skill.enabled else "\u2717"
        desc_full = skill.description or ""
        desc = desc_full[:50] + "..." if len(desc_full) > 50 else desc_full
        # Escape pipe characters for valid markdown table
        name_safe = skill.name.replace("|", "\\|")
        desc_safe = desc.replace("|", "\\|")
        lines.append(f"| {name_safe} | {desc_safe} | {category} | {enabled} |")

    return "\n".join(lines)


def recommend_skills_for_task(
    task: dict[str, Any] | None,
    db: HubDatabase | None = None,
    project_id: str | None = None,
) -> list[str]:
    """Recommend relevant skills based on task category.

    Uses HookSkillManager to get skill recommendations based on the task's
    category field. Returns always-apply skills if no category is set.

    Args:
        task: Task dict with optional 'category' field, or None.
        db: Optional database instance for DB-backed skill loading.
            When provided, skills are read from the unified DB instead
            of falling back to filesystem discovery.
        project_id: Optional project ID for loading project-scoped skills.

    Returns:
        List of recommended skill names for this task.
    """
    if task is None:
        return []

    from gobby.hooks.skill_manager import HookSkillManager

    try:
        manager = HookSkillManager(db=db, project_id=project_id)
        category = task.get("category")
        return manager.recommend_skills(category=category)
    except (ImportError, ValueError, KeyError, RuntimeError) as e:
        logger.debug(f"Failed to recommend skills (expected): {e}")
        return []
    except Exception as e:
        logger.warning(f"Unexpected error recommending skills: {e}")
        raise
