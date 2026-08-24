"""Handler for the update_skill tool."""

from __future__ import annotations

import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext

logger = logging.getLogger(__name__)


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the update_skill tool on the registry."""

    @registry.tool(
        name="update_skill",
        description="Update a skill by refreshing from its source. Returns whether the skill was updated.",
    )
    def update_skill(
        name: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Update a skill by refreshing from its source path.

        Args:
            name: Skill name (used if skill_id not provided)
            skill_id: Skill ID (takes precedence over name)

        Returns:
            Dict with success status and update info
        """
        try:
            # Validate input
            if not skill_id and not name:
                return {"success": False, "error": "Either name or skill_id is required"}

            # Find the skill first
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

            # Use SkillUpdater to refresh from source
            # (notifier triggers re-indexing automatically if updated)
            result = ctx.updater.update_skill(skill.id)

            if result.error:
                return {"success": False, "error": result.error}

            return {
                "success": True,
                "updated": result.updated,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
            }
        except Exception as e:
            logger.warning("Failed to update skill: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}
