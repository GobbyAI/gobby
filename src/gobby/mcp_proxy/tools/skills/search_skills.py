"""Handler for the search_skills tool and search indexing."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext
from gobby.skills.search import SearchFilters
from gobby.storage.skills import ChangeEvent

logger = logging.getLogger(__name__)

# Upper bound for skill index queries -- high enough to get all installed
# skills without unbounded queries. Actual counts are typically < 200.
_MAX_SKILL_INDEX = 10_000


class _SkillIndexer:
    """Manages skill search indexing with dirty-flag debouncing.

    Marks the index dirty on skill mutations. The index is rebuilt lazily
    at search time, coalescing rapid mutations into a single rebuild.
    """

    def __init__(self, ctx: SkillsContext) -> None:
        self._ctx = ctx
        self._dirty = False
        self._lock = threading.Lock()

    def build(self) -> None:
        """Rebuild the search index from all skills."""
        skills = self._ctx.storage.list_skills(
            project_id=self._ctx.project_id,
            limit=_MAX_SKILL_INDEX,
            include_global=True,
        )
        self._ctx.search.index_skills(skills)
        with self._lock:
            self._dirty = False

    def mark_dirty(self, event: ChangeEvent) -> None:
        """Mark index as stale (called on skill mutations)."""
        with self._lock:
            self._dirty = True

    def ensure_fresh(self) -> None:
        """Rebuild index if dirty. Called before searches."""
        with self._lock:
            needs_rebuild = self._dirty
        if needs_rebuild:
            self.build()


def _setup_indexing(ctx: SkillsContext) -> _SkillIndexer:
    """Index all skills and wire up change-driven re-indexing.

    Returns the indexer so the search tool can call ensure_fresh().
    """
    indexer = _SkillIndexer(ctx)
    indexer.build()
    ctx.notifier.add_listener(indexer.mark_dirty)
    return indexer


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the search_skills tool on the registry and set up indexing."""
    indexer = _setup_indexing(ctx)

    @registry.tool(
        name="search_skills",
        description="Search for skills by query. Returns ranked results with relevance scores. Supports filtering by category and tags. Internal methodology skills (frontmatter `internal: true`) are hidden by default; pass include_internal=true to include them.",
    )
    async def search_skills(
        query: str,
        category: str | None = None,
        tags_any: list[str] | None = None,
        tags_all: list[str] | None = None,
        top_k: int = 10,
        session_id: str | None = None,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        """
        Search for skills by natural language query.

        Returns ranked results with relevance scores.

        Args:
            query: Search query (required, non-empty)
            category: Optional category filter
            tags_any: Optional tags filter - match any of these tags
            tags_all: Optional tags filter - match all of these tags
            top_k: Maximum results to return (default 10)
            include_internal: If True, include skills flagged `internal: true` in
                frontmatter. Default False hides them — they are shared-methodology
                skills invoked by other skills via get_skill(name=...), not user-facing.

        Returns:
            Dict with success status and ranked search results
        """
        try:
            # Validate query
            if not query or not query.strip():
                return {"success": False, "error": "Query is required and cannot be empty"}

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
                except Exception as sv_err:
                    logger.warning(
                        f"Failed to resolve session variables for {session_id}: {sv_err}"
                    )

            # Build filters
            filters = None
            if category or tags_any or tags_all or active_names is not None:
                filters = SearchFilters(
                    category=category,
                    tags_any=tags_any,
                    tags_all=tags_all,
                    allowed_names=active_names,
                )

            # Ensure index is fresh before searching (run in thread to avoid blocking event loop)
            await asyncio.to_thread(indexer.ensure_fresh)

            # Over-fetch when we'll post-filter internal skills, so top_k survives the trim.
            search_top_k = top_k * 3 if not include_internal else top_k
            results = await ctx.search.search_async(
                query=query, top_k=search_top_k, filters=filters
            )

            # Surface clear error when index was never built (startup failure)
            if not results and not ctx.search.index_attempted:
                return {
                    "success": False,
                    "error": "Skill search index is not available. Index has not been built.",
                }

            # Batch-fetch skills to avoid N+1 queries
            skill_ids = [r.skill_id for r in results]
            skills_by_id = {s.id: s for s in ctx.storage.get_skills_by_ids(skill_ids)}

            if not include_internal:
                # Keep a result when its skill is unknown (best-effort) OR not
                # flagged internal. Rewritten from a double-negative walrus
                # comprehension so the intent reads top-to-bottom.
                filtered_results = []
                for r in results:
                    skill = skills_by_id.get(r.skill_id)
                    if skill is None or not skill.is_internal():
                        filtered_results.append(r)
                results = filtered_results[:top_k]

            # Format results with skill metadata
            result_list = []
            for r in results:
                skill = skills_by_id.get(r.skill_id)

                # Get category and tags from metadata
                category_value = None
                tags = []
                if skill and skill.metadata and isinstance(skill.metadata, dict):
                    skillport = skill.metadata.get("skillport", {})
                    if isinstance(skillport, dict):
                        category_value = skillport.get("category")
                        tags = skillport.get("tags", [])

                result_list.append(
                    {
                        "skill_id": r.skill_id,
                        "skill_name": r.skill_name,
                        "description": skill.description if skill else None,
                        "category": category_value,
                        "tags": tags,
                        "score": r.similarity,
                    }
                )

            return {
                "success": True,
                "count": len(result_list),
                "results": result_list,
            }
        except Exception as e:
            logger.error(f"search_skills failed for query={query!r}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
