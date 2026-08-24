"""Handler for the restore_skill tool."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the restore_skill tool on the registry."""

    @registry.tool(
        name="restore_skill",
        description="Restore a soft-deleted skill.",
    )
    def restore_skill(
        skill_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """
        Restore a soft-deleted skill.

        Args:
            skill_id: Skill ID (takes precedence over name)
            name: Skill name

        Returns:
            Dict with success status and restored skill info
        """
        try:
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            skill = None
            if skill_id:
                try:
                    skill = ctx.storage.get_skill(skill_id, include_deleted=True)
                except ValueError:
                    pass

            if skill is None and name:
                skill = ctx.storage.get_by_name(
                    name,
                    project_id=ctx.project_id,
                    include_deleted=True,
                )

            if skill is None:
                return {"success": False, "error": f"Skill not found: {skill_id or name}"}

            if skill.deleted_at is None:
                return {"success": False, "error": f"Skill '{skill.name}' is not deleted"}

            restored = ctx.storage.restore(skill.id)
            return {
                "success": True,
                "restored": True,
                "skill_id": restored.id,
                "skill_name": restored.name,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
