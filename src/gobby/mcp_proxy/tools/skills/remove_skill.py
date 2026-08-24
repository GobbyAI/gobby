"""Handler for the remove_skill tool."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the remove_skill tool on the registry."""

    @registry.tool(
        name="remove_skill",
        description="Soft-delete a skill by name or ID. The skill can be restored later with restore_skill.",
    )
    def remove_skill(
        name: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Soft-delete a skill (sets deleted_at, can be restored).

        Args:
            name: Skill name (used if skill_id not provided)
            skill_id: Skill ID (takes precedence over name)

        Returns:
            Dict with success status and removed skill info
        """
        try:
            # Validate input
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            # Find the skill first to get its name
            skill = None
            if skill_id:
                try:
                    skill = ctx.storage.get_skill(skill_id)
                except ValueError:
                    pass

            if skill is None and name:
                skill = ctx.storage.get_by_name(name, project_id=ctx.project_id)

            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            # Store the name before deletion
            skill_name = skill.name

            # Soft-delete the skill (notifier triggers re-indexing automatically)
            ctx.storage.delete_skill(skill.id)

            return {
                "success": True,
                "removed": True,
                "skill_name": skill_name,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
