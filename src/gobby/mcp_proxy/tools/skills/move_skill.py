"""Handlers for move_skill_to_project and move_skill_to_installed tools."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the move_skill_to_project and move_skill_to_installed tools."""

    @registry.tool(
        name="move_skill_to_project",
        description="Move a skill to project scope.",
    )
    async def move_skill_to_project(
        skill_id: str,
        target_project_id: str,
    ) -> dict[str, Any]:
        """
        Move a skill to project scope.

        Args:
            skill_id: Skill ID to move
            target_project_id: Target project ID

        Returns:
            Dict with success status and moved skill info
        """
        try:
            skill = await ctx.run_db(ctx.storage.move_to_project, skill_id, target_project_id)
            return {
                "success": True,
                "skill_id": skill.id,
                "skill_name": skill.name,
                "source": skill.source,
                "project_id": skill.project_id,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="move_skill_to_installed",
        description="Move a project-scoped skill back to installed scope.",
    )
    async def move_skill_to_installed(
        skill_id: str,
    ) -> dict[str, Any]:
        """
        Move a project-scoped skill back to installed scope.

        Args:
            skill_id: Skill ID to move

        Returns:
            Dict with success status and moved skill info
        """
        try:
            skill = await ctx.run_db(ctx.storage.move_to_installed, skill_id)
            return {
                "success": True,
                "skill_id": skill.id,
                "skill_name": skill.name,
                "source": skill.source,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
