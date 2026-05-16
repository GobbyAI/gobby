"""Tests for search_skills MCP tool (TDD - written before implementation)."""

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.migrations import run_migrations
from gobby.storage.skills import LocalSkillManager

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path: Path) -> Iterator[LocalDatabase]:
    """Create a fresh database with migrations applied."""
    db_path = tmp_path / "test.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def storage(db: LocalDatabase) -> LocalSkillManager:
    """Create a LocalSkillManager for storage operations."""
    return LocalSkillManager(db)


@pytest.fixture
def populated_db(db: LocalDatabase, storage: LocalSkillManager) -> LocalDatabase:
    """Create database with test skills for search."""
    storage.create_skill(
        name="git-commit",
        description="Generate conventional commit messages for git repositories",
        content="# Git Commit Helper\n\nHelps write good commit messages.",
        metadata={"skillport": {"category": "git", "tags": ["git", "commits", "version-control"]}},
        enabled=True,
    )
    storage.create_skill(
        name="git-rebase",
        description="Interactive git rebase assistant",
        content="# Git Rebase\n\nHelps with rebasing branches.",
        metadata={"skillport": {"category": "git", "tags": ["git", "rebase"]}},
        enabled=True,
    )
    storage.create_skill(
        name="code-review",
        description="AI-powered code review for pull requests",
        content="# Code Review\n\nReviews code quality.",
        metadata={"skillport": {"category": "code-quality", "tags": ["review", "quality", "pr"]}},
        enabled=True,
    )
    storage.create_skill(
        name="python-typing",
        description="Add type hints to Python code",
        content="# Python Typing\n\nAdds type annotations.",
        metadata={"skillport": {"category": "python", "tags": ["python", "typing", "quality"]}},
        enabled=True,
    )
    return db


@pytest.fixture
async def registry(populated_db):
    """Create a registry with search index fully built."""
    from gobby.mcp_proxy.tools.skills import create_skills_registry
    from gobby.storage.skills import LocalSkillManager

    registry = create_skills_registry(populated_db)

    # Ensure indexing is complete
    storage = LocalSkillManager(populated_db)
    skills = storage.list_skills(limit=1000, include_global=True)
    if hasattr(registry, "search"):
        await registry.search.index_skills_async(skills)

    return registry


@pytest.fixture
async def empty_registry(db):
    """Create a registry with no skills indexed (for no-match tests)."""
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    registry = create_skills_registry(db)
    # Clear the search index to ensure no skills are indexed
    if hasattr(registry, "search"):
        registry.search.clear()

    return registry


class TestSearchSkillsTool:
    """Tests for search_skills MCP tool."""

    @pytest.mark.asyncio
    async def test_search_skills_returns_results(self, registry):
        """Test that search_skills returns matching results."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="git commit")

        assert result["success"] is True
        assert result["count"] > 0
        assert len(result["results"]) > 0

    @pytest.mark.asyncio
    async def test_repeated_search_skills_keeps_sqlite_connections_bounded(self, populated_db):
        """Search index refresh and result hydration use the injected DB runner."""
        from gobby.mcp_proxy.tools.skills import create_skills_registry

        executor = DatabaseExecutor(max_workers=2, thread_name_prefix="skills-search-db")
        original_get_skills_by_ids = LocalSkillManager.get_skills_by_ids

        def slow_get_skills_by_ids(self: LocalSkillManager, *args: Any, **kwargs: Any) -> Any:
            """Delay result hydration so bounded DB runner behavior is observable."""
            time.sleep(0.02)
            return original_get_skills_by_ids(self, *args, **kwargs)

        try:
            registry = create_skills_registry(populated_db, run_db=executor.run)
            tool = registry.get_tool("search_skills")
            first = await tool(query="git")
            assert first["success"] is True

            with patch.object(
                LocalSkillManager,
                "get_skills_by_ids",
                new=slow_get_skills_by_ids,
            ):
                results = await asyncio.gather(*(tool(query="git") for _ in range(20)))

            assert all(result["success"] is True for result in results)
            assert populated_db.connection_count <= 1 + executor.max_workers
        finally:
            executor.shutdown(wait=True)

    @pytest.mark.asyncio
    async def test_search_skills_returns_scores(self, registry):
        """Test that search results include relevance scores."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="git")

        assert result["success"] is True
        for res in result["results"]:
            assert "score" in res
            assert isinstance(res["score"], (int, float))
            assert res["score"] >= 0

    @pytest.mark.asyncio
    async def test_search_skills_ranked_by_relevance(self, registry):
        """Test that results are ranked by relevance."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="git commit message")

        assert result["success"] is True
        # Results should be sorted by score descending
        scores = [r["score"] for r in result["results"]]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_search_skills_respects_top_k(self, registry):
        """Test that search respects top_k limit."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="git", top_k=1)

        assert result["success"] is True
        assert result["count"] <= 1

    @pytest.mark.asyncio
    async def test_search_skills_filters_by_category(self, registry):
        """Test that search filters by category."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="code", category="code-quality")

        assert result["success"] is True
        for res in result["results"]:
            assert res["category"] == "code-quality"

    @pytest.mark.asyncio
    async def test_search_skills_filters_by_tags_any(self, registry):
        """Test that search filters by any of the specified tags."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="code", tags_any=["quality", "typing"])

        assert result["success"] is True
        # All results should have at least one of the tags
        for res in result["results"]:
            assert any(tag in res["tags"] for tag in ["quality", "typing"])

    @pytest.mark.asyncio
    async def test_search_skills_filters_by_tags_all(self, registry):
        """Test that search filters by all of the specified tags."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="git", tags_all=["git", "commits"])

        assert result["success"] is True
        # All results should have all the tags
        for res in result["results"]:
            assert "git" in res["tags"]
            assert "commits" in res["tags"]

    @pytest.mark.asyncio
    async def test_search_skills_empty_query(self, registry):
        """Test search with empty query returns error."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="")

        assert result["success"] is False
        assert "query" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_search_skills_no_matches(self, empty_registry):
        """Test search with no indexed skills returns empty results."""
        tool = empty_registry.get_tool("search_skills")

        result = await tool(query="nonexistent gibberish xyz")

        assert result["success"] is True
        assert result["count"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_search_skills_returns_skill_metadata(self, registry):
        """Test that search results include skill metadata."""
        tool = registry.get_tool("search_skills")

        result = await tool(query="git commit")

        assert result["success"] is True
        assert len(result["results"]) > 0

        res = result["results"][0]
        assert "skill_id" in res
        assert "skill_name" in res
        assert "description" in res
        assert "category" in res
        assert "tags" in res

    @pytest.mark.asyncio
    async def test_search_skills_combined_filters(self, registry):
        """Test search with multiple filters."""
        tool = registry.get_tool("search_skills")

        result = await tool(
            query="code quality",
            category="python",
            tags_any=["typing"],
        )

        assert result["success"] is True
        for res in result["results"]:
            assert res["category"] == "python"
            assert "typing" in res["tags"]


@pytest.fixture
async def registry_with_internal(db, storage):
    """Registry with a mix of public and internal-flagged methodology skills."""
    storage.create_skill(
        name="plan-user",
        description="User-facing planning skill for workflow orchestration",
        content="# Plan user-facing\n\nDrives /plan command.",
        metadata={"skillport": {"category": "core", "tags": ["plan"]}},
        enabled=True,
    )
    storage.create_skill(
        name="plan-methodology-alpha",
        description="Internal plan drafting methodology shared across agents",
        content="# Methodology Alpha\n\nInternal drafting rules.",
        metadata={"internal": True, "skillport": {"category": "core", "tags": ["plan"]}},
        enabled=True,
    )
    storage.create_skill(
        name="plan-methodology-beta",
        description="Another internal plan methodology, tagged via gobby namespace",
        content="# Methodology Beta\n\nInternal review rules.",
        metadata={
            "gobby": {"internal": True},
            "skillport": {"category": "core", "tags": ["plan"]},
        },
        enabled=True,
    )

    from gobby.mcp_proxy.tools.skills import create_skills_registry

    registry = create_skills_registry(db)
    skills = storage.list_skills(limit=1000, include_global=True)
    if hasattr(registry, "search"):
        await registry.search.index_skills_async(skills)
    return registry


class TestSearchSkillsInternalFilter:
    """Filter behavior for `internal: true` skills in search."""

    @pytest.mark.asyncio
    async def test_search_hides_internal_by_default(self, registry_with_internal):
        tool = registry_with_internal.get_tool("search_skills")

        result = await tool(query="plan")

        assert result["success"] is True
        names = {r["skill_name"] for r in result["results"]}
        assert "plan-user" in names
        assert "plan-methodology-alpha" not in names
        assert "plan-methodology-beta" not in names

    @pytest.mark.asyncio
    async def test_search_include_internal_surfaces_them(self, registry_with_internal):
        tool = registry_with_internal.get_tool("search_skills")

        result = await tool(query="plan", include_internal=True)

        assert result["success"] is True
        names = {r["skill_name"] for r in result["results"]}
        assert {"plan-user", "plan-methodology-alpha", "plan-methodology-beta"}.issubset(names)
