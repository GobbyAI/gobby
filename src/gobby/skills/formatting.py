"""Skill formatting helpers.

Functions for rendering skill lists, active skill manifests, and fetch directives.
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


def skill_fetch_directive(name: str) -> str:
    """Return the canonical agent-facing directive for loading a skill."""
    return f"Call get_skill(name={json.dumps(name)}) on gobby-skills, then continue."


def format_skill_fetch_context(name: str, args: str | None = None) -> str:
    """Return a skill fetch directive with optional user arguments preserved."""
    parts = [skill_fetch_directive(name)]
    if args:
        parts.append(f"User arguments: {args}")
    return "\n\n".join(parts)


def active_skill_manifest(skills: Sequence[Any]) -> str:
    """Render an agent-visible manifest of active skills without skill bodies."""
    lines = ["<active_skills>"]
    seen: set[str] = set()
    for skill in skills:
        name = getattr(skill, "name", None)
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        lines.append(f"- name: {name}")
        lines.append(f"  ref: gobby-skills:get_skill name={json.dumps(name)}")
    lines.append("</active_skills>")
    return "\n".join(lines) if seen else ""


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

    Emits only an active-skill manifest. Full skill bodies are loaded on demand
    through gobby-skills:get_skill so hooks never inline skill content.

    Args:
        skills_with_formats: List of (ParsedSkill, resolved_format) tuples

    Returns:
        Active skill manifest, or empty string if nothing was rendered.
    """
    return active_skill_manifest([skill for skill, _fmt in skills_with_formats])


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
