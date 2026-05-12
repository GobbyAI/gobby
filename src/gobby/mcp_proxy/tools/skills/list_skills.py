"""Handler for the list_skills tool."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext

logger = logging.getLogger(__name__)

_MAX_OVERFETCH_ROUNDS = 3


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the list_skills tool on the registry."""

    @registry.tool(
        name="list_skills",
        description="List all skills with lightweight metadata. Supports filtering by category and enabled status. Internal methodology skills (frontmatter `internal: true`) are hidden by default; pass include_internal=true to surface them.",
    )
    async def list_skills(
        category: str | None = None,
        enabled: bool | None = None,
        limit: int = 50,
        session_id: str | None = None,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        """
        List skills with lightweight metadata.

        Returns ~100 tokens per skill: name, description, category, tags, enabled, source.
        Does NOT include content, allowed_tools, or compatibility.

        Args:
            category: Optional category filter
            enabled: Optional enabled status filter (True/False/None for all)
            limit: Maximum skills to return (default 50)
            session_id: Optional session ID for filtering by active skills in the session
            include_internal: If True, include skills flagged `internal: true` in
                frontmatter. Default False hides them — they are shared-methodology
                skills invoked by other skills via get_skill(name=...), not user-facing.

        Returns:
            Dict with success status and list of skill metadata
        """
        try:
            active_names = None
            if session_id:
                try:
                    from gobby.workflows.state_manager import SessionVariableManager

                    resolved_id = ctx.session_manager.resolve_session_reference(
                        session_id, project_id=ctx.project_id
                    )
                    sv_mgr = SessionVariableManager(ctx.db)
                    sv = sv_mgr.get_variables(resolved_id)
                    active_names = sv.get("_active_skill_names") if sv else None
                except Exception:
                    logger.debug(f"Failed to resolve active skill names for session {session_id}")

            # Over-fetch when a post-query filter (active_names or include_internal=False)
            # will trim results, so we can still fill `limit` after filtering.
            #
            # The fixed multiplier in the old implementation under-delivered when
            # BOTH filters were active and aggressive (e.g. an active-skills
            # allowlist of 5 names on a project with hundreds of internal skills).
            # Fetch in bounded pages and stop as soon as we have enough post-filter
            # results or the underlying storage reports EOF.
            needs_overfetch = active_names is not None or not include_internal
            active_set = set(active_names) if active_names is not None else None

            def _apply_post_filters(batch: list[Any]) -> list[Any]:
                filtered = batch
                if not include_internal:
                    filtered = [s for s in filtered if not s.is_internal()]
                if active_set is not None:
                    filtered = [s for s in filtered if s.name in active_set]
                return filtered

            async def _list_skills_batch(
                *,
                limit_value: int,
                offset_value: int = 0,
            ) -> list[Any]:
                return await asyncio.to_thread(
                    ctx.storage.list_skills,
                    project_id=ctx.project_id,
                    category=category,
                    enabled=enabled,
                    limit=limit_value,
                    offset=offset_value,
                    include_global=True,
                )

            skills: list[Any] = []
            if not needs_overfetch:
                skills = await _list_skills_batch(limit_value=limit)
            else:
                page_limit = limit * 5
                offset = 0
                for _ in range(_MAX_OVERFETCH_ROUNDS):
                    batch = await _list_skills_batch(
                        limit_value=page_limit,
                        offset_value=offset,
                    )
                    if not batch:
                        break
                    skills.extend(_apply_post_filters(batch))
                    if len(batch) < page_limit:
                        # EOF from storage — no more pages to scan.
                        break
                    if len(skills) >= limit:
                        break
                    offset += page_limit

            skills = skills[:limit]

            # Extract lightweight metadata only
            skill_list = []
            for skill in skills:
                # Get category and tags from metadata
                category_value = None
                tags = []
                if skill.metadata and isinstance(skill.metadata, dict):
                    skillport = skill.metadata.get("skillport", {})
                    if isinstance(skillport, dict):
                        category_value = skillport.get("category")
                        tags = skillport.get("tags", [])

                skill_list.append(
                    {
                        "id": skill.id,
                        "name": skill.name,
                        "description": skill.description,
                        "category": category_value,
                        "tags": tags,
                        "enabled": skill.enabled,
                        "source": skill.source,
                    }
                )

            return {
                "success": True,
                "count": len(skill_list),
                "skills": skill_list,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
