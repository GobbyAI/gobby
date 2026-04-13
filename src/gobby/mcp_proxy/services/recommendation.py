"""Recommendation service."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from gobby.config.features import RecommendToolsConfig
from gobby.prompts import PromptLoader
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger("gobby.mcp.server")

# Search mode type
SearchMode = Literal["llm", "semantic", "hybrid"]


class RecommendationService:
    """Service for recommending tools."""

    def __init__(
        self,
        llm_service: Any,
        mcp_manager: Any,
        semantic_search: Any | None = None,
        project_id: str | None = None,
        config: RecommendToolsConfig | None = None,
        db: DatabaseProtocol | None = None,
    ):
        self._llm_service = llm_service
        self._mcp_manager = mcp_manager
        self._semantic_search = semantic_search
        self._project_id = project_id
        self._config = config
        self._loader = PromptLoader(db=db)

    def _get_config(self) -> RecommendToolsConfig:
        """Get config with fallback to defaults."""
        if self._config is not None:
            return self._config
        from gobby.config.features import RecommendToolsConfig

        return RecommendToolsConfig()

    async def recommend_tools(
        self,
        task_description: str,
        agent_id: str | None = None,
        search_mode: SearchMode = "llm",
        top_k: int = 10,
        min_similarity: float = 0.3,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Recommend tools based on task description.

        Args:
            task_description: Description of what the user wants to do
            agent_id: Optional agent ID for filtering (reserved for future use)
            search_mode: How to search for tools:
                - "llm": Use LLM to recommend (default, original behavior)
                - "semantic": Use embedding similarity search
                - "hybrid": Combine semantic search with LLM ranking
            top_k: Maximum recommendations to return (for semantic/hybrid)
            min_similarity: Minimum similarity threshold (for semantic/hybrid)
            project_id: Project ID for semantic/hybrid search (overrides instance default)

        Returns:
            Dict with recommendations and metadata
        """
        # Use provided project_id or fall back to instance default
        effective_project_id = project_id or self._project_id

        if search_mode == "semantic":
            return await self._recommend_semantic(
                task_description, top_k, min_similarity, effective_project_id
            )
        elif search_mode == "hybrid":
            return await self._recommend_hybrid(
                task_description, top_k, min_similarity, effective_project_id
            )
        else:
            return await self._recommend_llm(task_description)

    async def _recommend_semantic(
        self, task_description: str, top_k: int, min_similarity: float, project_id: str | None
    ) -> dict[str, Any]:
        """Recommend tools using semantic similarity search."""
        if not self._semantic_search:
            return {
                "success": False,
                "error": "Semantic search not configured",
                "task": task_description,
            }

        if not project_id:
            return {
                "success": False,
                "error": "Project ID not set for semantic search",
                "task": task_description,
            }

        try:
            results = await self._semantic_search.search_tools(
                query=task_description,
                project_id=project_id,
                top_k=top_k,
                min_similarity=min_similarity,
            )

            recommendations = [
                {
                    "server": r.server_name,
                    "tool": r.tool_name,
                    "reason": r.description or "Semantically similar to query",
                    "similarity": round(r.similarity, 4),
                }
                for r in results
            ]

            return {
                "success": True,
                "task": task_description,
                "search_mode": "semantic",
                "recommendation": recommendations,
                "recommendations": recommendations,
                "total_results": len(results),
            }
        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return {"success": False, "error": str(e), "task": task_description}

    async def _recommend_hybrid(
        self, task_description: str, top_k: int, min_similarity: float, project_id: str | None
    ) -> dict[str, Any]:
        """Recommend tools using semantic search + LLM re-ranking."""
        # First get semantic results
        semantic_result = await self._recommend_semantic(
            task_description,
            top_k * 2,
            min_similarity,
            project_id,  # Get more for re-ranking
        )

        if not semantic_result.get("success") or not semantic_result.get("recommendations"):
            # Fall back to pure LLM if semantic fails
            return await self._recommend_llm(task_description)

        # Use LLM to re-rank and add reasoning
        try:
            config = self._get_config()
            candidates = semantic_result["recommendations"]
            candidate_list = "\n".join(
                f"- {c['server']}/{c['tool']}: {c.get('reason', 'No description')}"
                for c in candidates
            )

            prompt_path = config.hybrid_rerank_prompt_path or "features/recommend_hybrid"
            context = {
                "task_description": task_description,
                "candidate_list": candidate_list,
                "top_k": top_k,
            }
            prompt = self._loader.render(prompt_path, context)

            provider = self._llm_service.get_default_provider()
            response = await provider.generate_text(
                prompt,
                caller="mcp_proxy.recommendation.hybrid_rerank",
            )

            # Parse LLM response
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                response = response.split("```")[1].split("```")[0].strip()

            data = json.loads(response)
            recommendations = data.get("recommendations", [])[:top_k]

            return {
                "success": True,
                "task": task_description,
                "search_mode": "hybrid",
                "recommendation": recommendations,
                "recommendations": recommendations,
                "semantic_candidates": len(candidates),
            }
        except Exception as e:
            logger.warning(f"Hybrid LLM re-ranking failed, using semantic results: {e}")
            # Fall back to semantic results
            semantic_result["search_mode"] = "hybrid_fallback"
            return semantic_result

    async def _recommend_llm(self, task_description: str) -> dict[str, Any]:
        """Recommend tools using LLM (original behavior)."""
        try:
            config = self._get_config()
            available_servers = self._mcp_manager.get_available_servers()

            prompt_path = config.llm_prompt_path or "features/recommend_llm"
            context = {
                "task_description": task_description,
                "available_servers": ", ".join(available_servers),
            }
            prompt = self._loader.render(prompt_path, context)

            provider = self._llm_service.get_default_provider()
            response = await provider.generate_text(
                prompt,
                caller="mcp_proxy.recommendation.llm",
            )

            try:
                if "```json" in response:
                    response = response.split("```json")[1].split("```")[0].strip()
                elif "```" in response:
                    response = response.split("```")[1].split("```")[0].strip()

                data = json.loads(response)
                recommendations = data.get("recommendations", [])
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                recommendations = []
                logger.warning(f"Failed to parse LLM recommendation response: {e}")

            return {
                "success": True,
                "task": task_description,
                "search_mode": "llm",
                "recommendation": recommendations,
                "recommendations": recommendations,
                "available_servers": available_servers,
            }
        except Exception as e:
            logger.error(f"Error generating recommendations: {e}")
            return {"success": False, "error": str(e), "task": task_description}
