"""MCP adapter for installed skill script materialization."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.skills.materialization import (
    SkillMaterializationError,
    get_skill_script_materializer,
)


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the script materialization boundary."""
    materializer = get_skill_script_materializer(ctx.db, storage=ctx.storage)

    @registry.tool(
        name="materialize_skill_scripts",
        description="Materialize a skill's scripts into Gobby's content-addressed cache.",
    )
    async def materialize_tool(name: str) -> dict[str, Any]:
        try:
            result = await materializer.resolve(
                name,
                project_id=ctx.project_id,
                run_db=ctx.run_db,
            )
            return result.to_tool_result()
        except (OSError, SkillMaterializationError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
