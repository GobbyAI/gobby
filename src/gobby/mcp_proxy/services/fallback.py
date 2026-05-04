"""Tool fallback resolution service.

Provides alternative tool suggestions when a tool call fails,
using semantic similarity and success rate weighting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.memory.vectorstore import (
    VECTORSTORE_WARNING_INTERVAL_SECONDS,
    is_recoverable_vector_store_error,
    log_rate_limited_warning,
)

if TYPE_CHECKING:
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.mcp_proxy.semantic_search import SemanticToolSearch

logger = logging.getLogger(__name__)


@dataclass
class FallbackSuggestion:
    """A suggested alternative tool."""

    server_name: str
    tool_name: str
    description: str | None
    similarity: float
    success_rate: float | None
    score: float  # Combined ranking score

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "description": self.description,
            "similarity": round(self.similarity, 4),
            "success_rate": round(self.success_rate, 4) if self.success_rate else None,
            "score": round(self.score, 4),
        }


class ToolFallbackResolver:
    """
    Resolves alternative tools when a tool call fails.

    Uses semantic similarity search to find similar tools and
    weights results by historical success rate from metrics.
    """

    # Default weight for similarity vs success_rate in scoring
    DEFAULT_SIMILARITY_WEIGHT = 0.7
    DEFAULT_SUCCESS_WEIGHT = 0.3

    # Minimum similarity threshold for candidates
    DEFAULT_MIN_SIMILARITY = 0.3

    # Default success rate when no metrics available
    DEFAULT_SUCCESS_RATE = 0.5

    def __init__(
        self,
        semantic_search: SemanticToolSearch,
        metrics_manager: ToolMetricsManager | None = None,
        similarity_weight: float = DEFAULT_SIMILARITY_WEIGHT,
        success_weight: float = DEFAULT_SUCCESS_WEIGHT,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ):
        """
        Initialize the fallback resolver.

        Args:
            semantic_search: SemanticToolSearch instance for finding similar tools
            metrics_manager: Optional ToolMetricsManager for success rate data
            similarity_weight: Weight for similarity score (0-1)
            success_weight: Weight for success rate score (0-1)
            min_similarity: Minimum similarity threshold for candidates
        """
        self._semantic_search = semantic_search
        self._metrics_manager = metrics_manager
        self._similarity_weight = similarity_weight
        self._success_weight = success_weight
        self._min_similarity = min_similarity
        self._last_vector_store_warning_at = -VECTORSTORE_WARNING_INTERVAL_SECONDS

    async def find_alternatives(
        self,
        failed_tool_name: str,
        failed_tool_description: str | None = None,
        error_context: str | None = None,
        server_name: str | None = None,
        project_id: str | None = None,
        top_k: int = 5,
        exclude_failed: bool = True,
    ) -> list[FallbackSuggestion]:
        """
        Find alternative tools similar to a failed tool.

        Uses semantic search to find tools with similar descriptions,
        then weights by historical success rate.

        Args:
            failed_tool_name: Name of the tool that failed
            failed_tool_description: Description of the failed tool (if available)
            error_context: Error message or context for better matching
            server_name: Server the failed tool belongs to (for exclusion)
            project_id: Project ID for scoping the search
            top_k: Maximum number of suggestions to return
            exclude_failed: Whether to exclude the failed tool from results

        Returns:
            List of FallbackSuggestion sorted by combined score (descending)
        """
        if not project_id:
            logger.warning("No project_id provided for fallback search")
            return []

        # Build query from tool info and error context
        query = self._build_search_query(failed_tool_name, failed_tool_description, error_context)

        # Get semantically similar tools
        try:
            search_results = await self._semantic_search.search_tools(
                query=query,
                project_id=project_id,
                top_k=top_k * 2,  # Get extra for filtering
                min_similarity=self._min_similarity,
            )
        except Exception as e:
            if is_recoverable_vector_store_error(e):
                self._log_vector_store_failure(
                    "Semantic fallback search unavailable while VectorStore recovers", e
                )
            else:
                logger.error(f"Semantic search failed in fallback resolver: {e}")
            return []

        if not search_results:
            logger.debug(f"No semantic matches found for '{failed_tool_name}'")
            return []

        # Filter out the failed tool if requested
        if exclude_failed:
            search_results = [
                r
                for r in search_results
                if not (r.tool_name == failed_tool_name and r.server_name == server_name)
            ]

        # Enrich with success rates and compute combined scores
        suggestions = []
        for result in search_results[:top_k]:
            success_rate = self._get_success_rate(result.server_name, result.tool_name, project_id)

            score = self._compute_score(result.similarity, success_rate)

            suggestions.append(
                FallbackSuggestion(
                    server_name=result.server_name,
                    tool_name=result.tool_name,
                    description=result.description,
                    similarity=result.similarity,
                    success_rate=success_rate,
                    score=score,
                )
            )

        # Sort by combined score (descending)
        suggestions.sort(key=lambda s: s.score, reverse=True)

        logger.debug(f"Found {len(suggestions)} fallback suggestions for '{failed_tool_name}'")
        return suggestions

    def _log_vector_store_failure(self, message: str, error: BaseException) -> None:
        """Rate-limit known VectorStore availability warnings in fallback lookups."""
        self._last_vector_store_warning_at = log_rate_limited_warning(
            logger,
            self._last_vector_store_warning_at,
            message,
            error,
        )

    def _build_search_query(
        self,
        tool_name: str,
        description: str | None,
        error_context: str | None,
    ) -> str:
        """
        Build a search query from tool info and error context.

        Args:
            tool_name: Name of the failed tool
            description: Tool description
            error_context: Error message or context

        Returns:
            Search query string
        """
        parts = [f"Tool similar to: {tool_name}"]

        if description:
            parts.append(f"Description: {description}")

        if error_context:
            # Extract key terms from error, avoiding noise
            parts.append(f"Context: {error_context[:200]}")

        return "\n".join(parts)

    def _get_success_rate(self, server_name: str, tool_name: str, project_id: str) -> float | None:
        """
        Get success rate for a tool from metrics.

        Args:
            server_name: Server name
            tool_name: Tool name
            project_id: Project ID

        Returns:
            Success rate (0-1) or None if no metrics available
        """
        if not self._metrics_manager:
            return None

        try:
            return self._metrics_manager.get_tool_success_rate(
                server_name=server_name,
                tool_name=tool_name,
                project_id=project_id,
            )
        except Exception as e:
            logger.debug(f"Failed to get success rate for {server_name}/{tool_name}: {e}")
            return None

    def _compute_score(self, similarity: float, success_rate: float | None) -> float:
        """
        Compute combined ranking score.

        Score = (similarity * similarity_weight) + (success_rate * success_weight)

        When success_rate is None, uses default value to avoid penalizing
        tools without metrics history.

        Args:
            similarity: Cosine similarity score (0-1)
            success_rate: Historical success rate (0-1) or None

        Returns:
            Combined score (0-1)
        """
        effective_success_rate = (
            success_rate if success_rate is not None else self.DEFAULT_SUCCESS_RATE
        )

        return similarity * self._similarity_weight + effective_success_rate * self._success_weight

    async def find_alternatives_for_error(
        self,
        server_name: str,
        tool_name: str,
        error_message: str,
        project_id: str,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """
        Convenience method for call_tool integration.

        Takes error details and returns serialized suggestions.

        Args:
            server_name: Server where the tool failed
            tool_name: Name of the failed tool
            error_message: Error message from the failure
            project_id: Project ID
            top_k: Number of suggestions to return

        Returns:
            List of suggestion dictionaries ready for JSON response
        """
        # Try to get tool description from cached tools
        description = await self._get_tool_description(server_name, tool_name)

        suggestions = await self.find_alternatives(
            failed_tool_name=tool_name,
            failed_tool_description=description,
            error_context=error_message,
            server_name=server_name,
            project_id=project_id,
            top_k=top_k,
        )

        return [s.to_dict() for s in suggestions]

    async def _get_tool_description(self, server_name: str, tool_name: str) -> str | None:
        """
        Get tool description from semantic search's cached data.

        Args:
            server_name: Server name
            tool_name: Tool name

        Returns:
            Tool description or None
        """
        # Look up tool description from the database
        # We don't have project_id here, so we search all
        try:
            row = self._semantic_search.db.fetchone(
                """
                SELECT t.description
                FROM tools t
                JOIN mcp_servers s ON t.mcp_server_id = s.id
                WHERE s.name = ? AND t.name = ?
                LIMIT 1
                """,
                (server_name, tool_name),
            )
            return row["description"] if row else None
        except Exception:
            return None
