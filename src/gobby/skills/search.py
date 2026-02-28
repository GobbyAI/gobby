"""Skill search using unified search backend.

This module provides skill search functionality using the UnifiedSearcher
for TF-IDF, embedding, or hybrid search with automatic fallback.

Features:
- Indexes skills by name, description, tags, and category
- Post-search filtering by category and tags
- Automatic fallback from embedding to TF-IDF when API unavailable
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.search import SearchConfig, UnifiedSearcher

if TYPE_CHECKING:
    from gobby.storage.skills import Skill

logger = logging.getLogger(__name__)


@dataclass
class SearchFilters:
    """Filters to apply to search results.

    Filters are applied AFTER similarity ranking, so results maintain
    their relevance ordering within the filtered set.

    Attributes:
        category: Filter by skill category (exact match)
        tags_any: Filter to skills with ANY of these tags
        tags_all: Filter to skills with ALL of these tags
    """

    category: str | None = None
    tags_any: list[str] | None = None
    tags_all: list[str] | None = None
    allowed_names: list[str] | None = None


@dataclass
class SkillSearchResult:
    """A search result containing a skill ID and relevance score.

    Attributes:
        skill_id: ID of the matching skill
        skill_name: Name of the matching skill (for display)
        similarity: Relevance score in range [0, 1]
    """

    skill_id: str
    skill_name: str
    similarity: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "similarity": self.similarity,
        }


@dataclass
class _SkillMeta:
    """Internal metadata about a skill for filtering."""

    name: str
    category: str | None
    tags: list[str]


class SkillSearch:
    """Search skills using unified search with automatic fallback.

    Uses UnifiedSearcher to provide skill search with:
    - TF-IDF mode (always available)
    - Embedding mode (requires API key)
    - Auto mode (embedding with TF-IDF fallback)
    - Hybrid mode (combines both with weighted scores)

    Example usage:
        ```python
        from gobby.skills.search import SkillSearch
        from gobby.search import SearchConfig

        # Basic auto mode (embedding with fallback)
        config = SearchConfig(mode="auto")
        search = SkillSearch(config)
        await search.index_skills_async(skills)
        results = await search.search_async("git commit", top_k=5)

        # Check if using fallback
        if search.is_using_fallback():
            print(f"Using TF-IDF fallback: {search.get_fallback_reason()}")
        ```
    """

    def __init__(
        self,
        config: SearchConfig | None = None,
        refit_threshold: int = 10,
    ):
        """Initialize skill search.

        Args:
            config: Search configuration (defaults to auto mode)
            refit_threshold: Number of updates before automatic refit
        """
        if config is None:
            config = SearchConfig(mode="auto")

        self._config = config
        self._refit_threshold = refit_threshold

        # Initialize unified searcher
        self._searcher = UnifiedSearcher(self._config)

        # Skill metadata tracking
        self._skill_names: dict[str, str] = {}  # skill_id -> skill_name
        self._skill_meta: dict[str, _SkillMeta] = {}  # skill_id -> metadata
        self._skill_items: list[tuple[str, str]] = []  # (skill_id, content) for reindexing

        # State tracking
        self._indexed = False
        self._pending_updates = 0

    @property
    def mode(self) -> str:
        """Return the current search mode."""
        return self._config.mode

    @property
    def tfidf_weight(self) -> float:
        """Return the TF-IDF weight for hybrid search."""
        return self._config.tfidf_weight

    @property
    def embedding_weight(self) -> float:
        """Return the embedding weight for hybrid search."""
        return self._config.embedding_weight

    def _build_search_content(self, skill: Skill) -> str:
        """Build searchable content from skill fields.

        Combines name, description, tags, and category into a single
        string for indexing.

        Args:
            skill: Skill to extract content from

        Returns:
            Combined search content string
        """
        parts = [
            skill.name,
            skill.description,
        ]

        # Add tags from metadata
        tags = skill.get_tags()
        if tags:
            parts.extend(tags)

        # Add category from metadata
        category = skill.get_category()
        if category:
            parts.append(category)

        return " ".join(parts)

    def index_skills(self, skills: list[Skill]) -> None:
        """Build search index from skills (sync wrapper).

        For async usage, prefer index_skills_async() instead.

        Args:
            skills: List of skills to index
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Can't use asyncio.run() inside a running loop
            # Use ThreadPoolExecutor to run in a separate thread and block until complete
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                # Defer coroutine construction to the executor thread
                future = executor.submit(lambda: asyncio.run(self.index_skills_async(skills)))
                future.result()
        else:
            asyncio.run(self.index_skills_async(skills))

    async def index_skills_async(self, skills: list[Skill]) -> None:
        """Build search index from skills.

        Indexes skills using the configured search mode (auto, tfidf,
        embedding, or hybrid).

        Args:
            skills: List of skills to index
        """
        if not skills:
            self._skill_names.clear()
            self._skill_meta.clear()
            self._skill_items = []
            self._indexed = False
            self._pending_updates = 0
            self._searcher.clear()
            logger.debug("Skill search index cleared (no skills)")
            return

        # Build (skill_id, content) tuples and metadata
        items: list[tuple[str, str]] = []
        self._skill_names.clear()
        self._skill_meta.clear()

        for skill in skills:
            content = self._build_search_content(skill)
            items.append((skill.id, content))
            self._skill_names[skill.id] = skill.name
            self._skill_meta[skill.id] = _SkillMeta(
                name=skill.name,
                category=skill.get_category(),
                tags=skill.get_tags(),
            )

        # Store for potential reindexing
        self._skill_items = items

        # Index using unified searcher
        await self._searcher.fit_async(items)
        self._indexed = True
        self._pending_updates = 0
        logger.info(f"Skill search index built with {len(skills)} skills")

    async def search_async(
        self,
        query: str,
        top_k: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SkillSearchResult]:
        """Search for skills matching the query.

        Uses the configured search mode with automatic fallback.

        Args:
            query: Search query text
            top_k: Maximum number of results to return
            filters: Optional filters to apply after ranking

        Returns:
            List of SkillSearchResult objects, sorted by similarity descending
        """
        if not self._indexed:
            return []

        # Get more results than top_k if filtering
        search_limit = top_k * 3 if filters else top_k
        raw_results = await self._searcher.search_async(query, top_k=search_limit)

        # Build results with filtering
        results = []
        for skill_id, similarity in raw_results:
            if filters and not self._passes_filters(skill_id, filters):
                continue

            skill_name = self._skill_names.get(skill_id, skill_id)
            results.append(
                SkillSearchResult(
                    skill_id=skill_id,
                    skill_name=skill_name,
                    similarity=similarity,
                )
            )

            if len(results) >= top_k:
                break

        return results

    def search(
        self,
        query: str,
        top_k: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SkillSearchResult]:
        """Search for skills matching the query (sync wrapper).

        For async usage, prefer search_async().

        Args:
            query: Search query text
            top_k: Maximum number of results to return
            filters: Optional filters to apply after ranking

        Returns:
            List of SkillSearchResult objects, sorted by similarity descending
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Can't use asyncio.run() inside a running loop
            # This is a best-effort sync wrapper; prefer search_async() in async contexts
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                # Defer coroutine construction to the executor thread
                future = executor.submit(
                    lambda: asyncio.run(self.search_async(query, top_k, filters))
                )
                return future.result()
        else:
            return asyncio.run(self.search_async(query, top_k, filters))

    def _passes_filters(self, skill_id: str, filters: SearchFilters) -> bool:
        """Check if a skill passes the given filters.

        Args:
            skill_id: ID of the skill to check
            filters: Filters to apply

        Returns:
            True if skill passes all filters
        """
        meta = self._skill_meta.get(skill_id)
        if not meta:
            return False

        # Check category filter
        if filters.category is not None:
            if meta.category != filters.category:
                return False

        # Check tags_any filter (skill must have at least one of the tags)
        if filters.tags_any is not None:
            if not any(tag in meta.tags for tag in filters.tags_any):
                return False

        # Check tags_all filter (skill must have all of the tags)
        if filters.tags_all is not None:
            if not all(tag in meta.tags for tag in filters.tags_all):
                return False

        # Check allowed_names filter (session-scoped allowlist)
        if filters.allowed_names is not None:
            if meta.name not in set(filters.allowed_names):
                return False

        return True

    def add_skill(self, skill: Skill) -> None:
        """Mark that a skill was added (requires reindex).

        Args:
            skill: The skill that was added
        """
        self._pending_updates += 1
        self._skill_names[skill.id] = skill.name
        self._skill_meta[skill.id] = _SkillMeta(
            name=skill.name,
            category=skill.get_category(),
            tags=skill.get_tags(),
        )
        self._searcher.mark_update()

    def update_skill(self, skill: Skill) -> None:
        """Mark that a skill was updated (requires reindex).

        Args:
            skill: The skill that was updated
        """
        self._pending_updates += 1
        self._skill_names[skill.id] = skill.name
        self._skill_meta[skill.id] = _SkillMeta(
            name=skill.name,
            category=skill.get_category(),
            tags=skill.get_tags(),
        )
        self._searcher.mark_update()

    def remove_skill(self, skill_id: str) -> None:
        """Mark that a skill was removed (requires reindex).

        Args:
            skill_id: ID of the skill that was removed
        """
        self._pending_updates += 1
        self._skill_names.pop(skill_id, None)
        self._skill_meta.pop(skill_id, None)
        self._searcher.mark_update()

    def needs_reindex(self) -> bool:
        """Check if the search index needs rebuilding.

        Returns:
            True if index_skills() should be called
        """
        if not self._indexed:
            return True
        return self._pending_updates >= self._refit_threshold or self._searcher.needs_refit()

    def is_using_fallback(self) -> bool:
        """Check if search is using TF-IDF fallback.

        Returns:
            True if using TF-IDF due to embedding failure
        """
        return self._searcher.is_using_fallback()

    def get_fallback_reason(self) -> str | None:
        """Get the reason for fallback, if any.

        Returns:
            Human-readable fallback reason, or None if not using fallback
        """
        return self._searcher.get_fallback_reason()

    def get_active_backend(self) -> str:
        """Get the name of the currently active backend.

        Returns:
            One of "tfidf", "embedding", "hybrid", or "none"
        """
        return self._searcher.get_active_backend()

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the search index.

        Returns:
            Dict with index statistics
        """
        stats: dict[str, Any] = {
            "indexed": self._indexed,
            "skill_count": len(self._skill_names),
            "pending_updates": self._pending_updates,
            "refit_threshold": self._refit_threshold,
            "active_backend": self.get_active_backend(),
            "using_fallback": self.is_using_fallback(),
        }

        # Add unified searcher stats
        searcher_stats = self._searcher.get_stats()
        stats.update(searcher_stats)

        return stats

    def clear(self) -> None:
        """Clear the search index."""
        self._searcher.clear()
        self._skill_names.clear()
        self._skill_meta.clear()
        self._skill_items = []
        self._indexed = False
        self._pending_updates = 0
