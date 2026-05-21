"""Tests for skill search functionality."""

import pytest

from gobby.skills.search import SearchFilters, SkillSearch, SkillSearchResult
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.skills import Skill

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    """Create a temporary LocalDatabase fixture."""
    database = LocalDatabase(tmp_path / "test.db")
    run_migrations(database)
    yield database
    database.close()


@pytest.fixture
def sample_skills():
    """Create sample skills for testing."""
    return [
        Skill(
            id="skl-commit",
            name="commit-message",
            description="Generate conventional commit messages",
            content="# Commit Messages\n\nInstructions...",
            metadata={
                "skillport": {
                    "category": "git",
                    "tags": ["git", "commits", "workflow"],
                }
            },
        ),
        Skill(
            id="skl-review",
            name="code-review",
            description="Perform thorough code reviews",
            content="# Code Review\n\nGuidelines...",
            metadata={
                "skillport": {
                    "category": "quality",
                    "tags": ["review", "quality", "best-practices"],
                }
            },
        ),
        Skill(
            id="skl-test",
            name="test-writing",
            description="Write comprehensive unit tests",
            content="# Testing\n\nTest patterns...",
            metadata={
                "skillport": {
                    "category": "testing",
                    "tags": ["testing", "unit-tests", "quality"],
                }
            },
        ),
        Skill(
            id="skl-git",
            name="git-workflow",
            description="Best practices for git branching and merging",
            content="# Git Workflow\n\nStrategies...",
            metadata={
                "skillport": {
                    "category": "git",
                    "tags": ["git", "branching", "workflow"],
                }
            },
        ),
    ]


class TestSkillSearchResult:
    """Tests for SkillSearchResult dataclass."""

    def test_creation(self) -> None:
        """Test creating a search result."""
        result = SkillSearchResult(
            skill_id="skl-test",
            skill_name="test-skill",
            similarity=0.85,
        )

        assert result.skill_id == "skl-test"
        assert result.skill_name == "test-skill"
        assert result.similarity == 0.85

    def test_to_dict(self) -> None:
        """Test converting to dictionary."""
        result = SkillSearchResult(
            skill_id="skl-test",
            skill_name="test-skill",
            similarity=0.75,
        )

        d = result.to_dict()
        assert d["skill_id"] == "skl-test"
        assert d["skill_name"] == "test-skill"
        assert d["similarity"] == 0.75


class TestSkillSearch:
    """Tests for SkillSearch class."""

    def test_index_empty_skills(self, db) -> None:
        """Test indexing empty skill list."""
        search = SkillSearch(db=db)
        search.index_skills([])

        assert not search._indexed
        assert search.search("query") == []

    def test_index_skills(self, db, sample_skills) -> None:
        """Test indexing skills."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        assert search._indexed
        assert len(search._skill_names) == 4

    def test_search_by_name(self, db, sample_skills) -> None:
        """Test searching by skill name."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        results = search.search("commit", top_k=5)

        assert len(results) > 0
        assert results[0].skill_name == "commit-message"
        assert results[0].similarity > 0

    def test_search_by_description(self, db, sample_skills) -> None:
        """Test searching by description content."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        results = search.search("unit tests", top_k=5)

        assert len(results) > 0
        # test-writing skill mentions "unit tests"
        skill_names = [r.skill_name for r in results]
        assert "test-writing" in skill_names

    def test_search_by_tags(self, db, sample_skills) -> None:
        """Test searching by tag content."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        results = search.search("workflow", top_k=5)

        assert len(results) > 0
        # Both commit-message and git-workflow have "workflow" tag
        skill_names = [r.skill_name for r in results]
        assert any("workflow" in name or "commit" in name for name in skill_names)

    def test_search_by_category(self, db, sample_skills) -> None:
        """Test searching by category."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        results = search.search("git", top_k=5)

        assert len(results) > 0
        # Both commit-message and git-workflow have "git" category
        skill_names = [r.skill_name for r in results]
        assert "git-workflow" in skill_names or "commit-message" in skill_names

    def test_search_returns_top_k(self, db, sample_skills) -> None:
        """Test that search respects top_k limit."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        results = search.search("quality", top_k=2)

        assert len(results) <= 2

    def test_search_results_ranked_by_similarity(self, db, sample_skills) -> None:
        """Test that results are sorted by similarity descending."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        results = search.search("code review quality", top_k=5)

        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i].similarity >= results[i + 1].similarity

    def test_search_no_results(self, db, sample_skills) -> None:
        """Test search with no matching results."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        results = search.search("xyznonexistent123", top_k=5)

        # May return empty or low-similarity results
        assert isinstance(results, list)

    def test_search_before_index(self, db) -> None:
        """Test searching before indexing returns empty."""
        search = SkillSearch(db=db)
        results = search.search("test")

        assert results == []

    def test_add_skill_marks_update(self, db, sample_skills) -> None:
        """Test that add_skill increments pending updates."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)
        assert search._pending_updates == 0

        new_skill = Skill(
            id="skl-new",
            name="new-skill",
            description="A new skill",
            content="Content",
        )
        search.add_skill(new_skill)

        assert search._pending_updates == 1
        assert search._skill_names["skl-new"] == "new-skill"

    def test_update_skill_marks_update(self, db, sample_skills) -> None:
        """Test that update_skill increments pending updates."""
        import copy

        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        updated_skill = copy.deepcopy(sample_skills[0])
        updated_skill.name = "updated-name"
        search.update_skill(updated_skill)

        assert search._pending_updates == 1

    def test_remove_skill_marks_update(self, db, sample_skills) -> None:
        """Test that remove_skill increments pending updates."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        search.remove_skill("skl-commit")

        assert search._pending_updates == 1
        assert "skl-commit" not in search._skill_names

    def test_needs_reindex_after_threshold(self, db, sample_skills) -> None:
        """Test that needs_reindex returns True after threshold updates."""
        search = SkillSearch(db=db, refit_threshold=3)
        search.index_skills(sample_skills)

        assert not search.needs_reindex()

        new_skill = Skill(id="skl-1", name="skill-1", description="Desc", content="C")
        search.add_skill(new_skill)
        assert not search.needs_reindex()

        search.add_skill(new_skill)
        assert not search.needs_reindex()

        search.add_skill(new_skill)
        assert search.needs_reindex()

    def test_needs_reindex_before_indexing(self, db) -> None:
        """Test that needs_reindex returns True before any indexing."""
        search = SkillSearch(db=db)
        assert search.needs_reindex()

    def test_get_stats(self, db, sample_skills) -> None:
        """Test getting search statistics."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        stats = search.get_stats()

        assert stats["indexed"] is True
        assert stats["skill_count"] == 4
        assert stats["pending_updates"] == 0
        assert "refit_threshold" in stats

    def test_clear(self, db, sample_skills) -> None:
        """Test clearing the search index."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)
        assert search._indexed

        search.clear()

        assert not search._indexed
        assert len(search._skill_names) == 0
        assert search._pending_updates == 0

    def test_custom_parameters(self, db, sample_skills) -> None:
        """Test creating search with custom parameters."""
        from gobby.search import SearchConfig

        config = SearchConfig(mode="keyword", keyword_weight=0.5, embedding_weight=0.5)
        search = SkillSearch(
            db=db,
            config=config,
            refit_threshold=5,
        )
        search.index_skills(sample_skills)

        stats = search.get_stats()
        assert stats["refit_threshold"] == 5
        assert stats["mode"] == "keyword"


class TestSkillSearchIntegration:
    """Integration tests for skill search with realistic scenarios."""

    def test_search_multiple_fields_combined(self, db, sample_skills) -> None:
        """Test that search considers all indexed fields."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        # Query that matches different fields in same skill
        results = search.search("git commit message workflow", top_k=5)

        assert len(results) > 0
        # commit-message skill matches name, tag, and category
        assert results[0].skill_id == "skl-commit"

    def test_reindex_after_updates(self, db, sample_skills) -> None:
        """Test reindexing after multiple updates."""
        search = SkillSearch(db=db, refit_threshold=2)
        search.index_skills(sample_skills)

        # Add skills to trigger reindex threshold
        for i in range(3):
            skill = Skill(
                id=f"skl-new-{i}",
                name=f"new-skill-{i}",
                description="New description",
                content="Content",
            )
            search.add_skill(skill)

        assert search.needs_reindex()

        # Re-index with updated list
        updated_skills = sample_skills + [
            Skill(
                id="skl-new-0",
                name="new-skill-0",
                description="Findable description about databases",
                content="Content",
            )
        ]
        search.index_skills(updated_skills)

        # Should find the new skill
        results = search.search("databases", top_k=5)
        skill_names = [r.skill_name for r in results]
        assert "new-skill-0" in skill_names


class TestSearchFilters:
    """Tests for SearchFilters dataclass (TDD - written before implementation)."""

    def test_create_empty_filters(self) -> None:
        """Test creating filters with no constraints."""
        filters = SearchFilters()
        assert filters.category is None
        assert filters.tags_any is None
        assert filters.tags_all is None

    def test_create_with_category(self) -> None:
        """Test creating filters with category."""
        filters = SearchFilters(category="git")
        assert filters.category == "git"

    def test_create_with_tags_any(self) -> None:
        """Test creating filters with tags_any."""
        filters = SearchFilters(tags_any=["git", "workflow"])
        assert filters.tags_any == ["git", "workflow"]

    def test_create_with_tags_all(self) -> None:
        """Test creating filters with tags_all."""
        filters = SearchFilters(tags_all=["git", "workflow"])
        assert filters.tags_all == ["git", "workflow"]

    def test_create_with_all_options(self) -> None:
        """Test creating filters with all options."""
        filters = SearchFilters(
            category="git",
            tags_any=["git"],
            tags_all=["workflow"],
        )
        assert filters.category == "git"
        assert filters.tags_any == ["git"]
        assert filters.tags_all == ["workflow"]


class TestSkillSearchFiltering:
    """Tests for skill search filtering (TDD - written before implementation)."""

    def test_filter_by_category(self, db, sample_skills) -> None:
        """Test filtering search results by category."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        # Search for general term that would match multiple skills
        filters = SearchFilters(category="git")
        results = search.search("workflow", top_k=10, filters=filters)

        # Only skills with category="git" should be returned
        for result in results:
            skill = next(s for s in sample_skills if s.id == result.skill_id)
            assert skill.get_category() == "git"

    def test_filter_by_category_no_matches(self, db, sample_skills) -> None:
        """Test filtering by category with no matches."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        filters = SearchFilters(category="nonexistent")
        results = search.search("git", top_k=10, filters=filters)

        assert results == []

    def test_filter_by_tags_any(self, db, sample_skills) -> None:
        """Test filtering by any of the specified tags."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        # "quality" tag exists in code-review and test-writing
        filters = SearchFilters(tags_any=["quality", "nonexistent"])
        results = search.search("code", top_k=10, filters=filters)

        for result in results:
            skill = next(s for s in sample_skills if s.id == result.skill_id)
            skill_tags = skill.get_tags()
            assert any(tag in skill_tags for tag in ["quality", "nonexistent"])

    def test_filter_by_tags_all(self, db, sample_skills) -> None:
        """Test filtering by all of the specified tags."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        # Only commit-message has both "git" AND "workflow" tags
        filters = SearchFilters(tags_all=["git", "workflow"])
        results = search.search("commit", top_k=10, filters=filters)

        for result in results:
            skill = next(s for s in sample_skills if s.id == result.skill_id)
            skill_tags = skill.get_tags()
            assert "git" in skill_tags and "workflow" in skill_tags

    def test_filter_combined_category_and_tags(self, db, sample_skills) -> None:
        """Test filtering by both category and tags."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        # category="git" AND has tag "commits"
        filters = SearchFilters(category="git", tags_any=["commits"])
        results = search.search("message", top_k=10, filters=filters)

        for result in results:
            skill = next(s for s in sample_skills if s.id == result.skill_id)
            assert skill.get_category() == "git"
            assert "commits" in skill.get_tags()

    def test_filters_applied_after_ranking(self, db, sample_skills) -> None:
        """Test that filters are applied after similarity ranking."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        # Get unfiltered results for comparison
        unfiltered = search.search("git", top_k=10)

        # Filter to only git category
        filters = SearchFilters(category="git")
        filtered = search.search("git", top_k=10, filters=filters)

        # Filtered results should be a subset
        filtered_ids = {r.skill_id for r in filtered}
        unfiltered_ids = {r.skill_id for r in unfiltered}
        assert filtered_ids.issubset(unfiltered_ids)

        # Filtered results should maintain relative ordering
        if len(filtered) > 1:
            for i in range(len(filtered) - 1):
                assert filtered[i].similarity >= filtered[i + 1].similarity

    def test_search_without_filters(self, db, sample_skills) -> None:
        """Test that search without filters returns all matching results."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        # No filters - should work as before
        results = search.search("git", top_k=10)
        assert len(results) > 0

    def test_empty_filters_same_as_no_filters(self, db, sample_skills) -> None:
        """Test that empty filters behave same as no filters."""
        search = SkillSearch(db=db)
        search.index_skills(sample_skills)

        no_filter_results = search.search("git", top_k=10)
        empty_filter_results = search.search("git", top_k=10, filters=SearchFilters())

        # Should return same results
        assert len(no_filter_results) == len(empty_filter_results)
        for r1, r2 in zip(no_filter_results, empty_filter_results, strict=False):
            assert r1.skill_id == r2.skill_id
