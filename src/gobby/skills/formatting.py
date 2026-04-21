"""Skill formatting helpers.

Functions for rendering skill lists as JSON, markdown tables, and injection formats.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from gobby.skills.metadata import get_skill_category, get_skill_tags

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


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


def render_skills_for_context(skills_with_formats: list[tuple[Any, str]]) -> str:
    """Format skills with pre-resolved injection formats.

    Only emits skills resolved to ``full`` or ``content`` format — skills at
    ``summary`` are dropped. Summary-tier injection is redundant with MCP
    progressive discovery (``list_skills`` / ``search_skills`` on
    ``gobby-skills``) and should be handled by targeted rules, not eager
    context dumps.

    Args:
        skills_with_formats: List of (ParsedSkill, resolved_format) tuples

    Returns:
        Formatted markdown string with skill content, or empty string if no
        skill was resolved to an expanded format.
    """
    expanded_sections: list[str] = []

    for skill, fmt in skills_with_formats:
        name = getattr(skill, "name", "unknown")
        description = getattr(skill, "description", "")
        content = getattr(skill, "content", "")

        if fmt == "full":
            section_lines = [f"### {name}"]
            if description:
                section_lines.append(description)
            if content:
                section_lines.append("")
                section_lines.append(content)
            expanded_sections.append("\n".join(section_lines))
        elif fmt == "content" and content:
            expanded_sections.append(content)

    return "\n\n".join(expanded_sections)


# Backwards-compatible aliases
_format_skills_with_formats = render_skills_for_context
format_skills_with_formats = render_skills_for_context


def recommend_skills_for_task(
    task: dict[str, Any] | None,
    db: DatabaseProtocol | None = None,
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
