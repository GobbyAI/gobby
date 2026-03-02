"""Tests for HookSkillManager."""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.integration


class TestHookSkillManager:
    """Tests for HookSkillManager class."""

    def test_import(self) -> None:
        """Test that HookSkillManager can be imported."""
        from gobby.hooks.skill_manager import HookSkillManager

        assert HookSkillManager is not None

    def test_instantiate(self) -> None:
        """Test that HookSkillManager can be instantiated."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        assert manager is not None

    def test_has_discover_core_skills_method(self) -> None:
        """Test that HookSkillManager has discover_core_skills method."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        assert hasattr(manager, "discover_core_skills")
        assert callable(manager.discover_core_skills)

    def test_discover_core_skills_returns_list(self) -> None:
        """Test that discover_core_skills returns a list."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        result = manager.discover_core_skills()
        assert isinstance(result, list)

    def test_discover_core_skills_loads_from_shared_path(self) -> None:
        """Test that discover_core_skills loads from install/shared/skills/."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        skills = manager.discover_core_skills()

        # Should find the built-in skills
        skill_names = [s.name for s in skills]
        assert len(skill_names) > 0  # At least some skills exist

    def test_discover_core_skills_finds_memory_skill(self) -> None:
        """Test that discover_core_skills finds the memory skill."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        skills = manager.discover_core_skills()

        skill_names = [s.name for s in skills]
        assert "memory" in skill_names

    def test_discovered_skills_have_name_and_content(self) -> None:
        """Test that discovered skills have name and content attributes."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        skills = manager.discover_core_skills()

        # Ensure we have at least one skill to test
        assert skills, "Expected at least one skill to be discovered"

        skill = skills[0]
        assert hasattr(skill, "name")
        assert hasattr(skill, "content")
        assert skill.name is not None
        assert skill.content is not None

    def test_get_skill_by_name(self) -> None:
        """Test getting a skill by name."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        skill = manager.get_skill_by_name("committing-changes")

        assert skill is not None
        assert skill.name == "committing-changes"

    def test_get_skill_by_name_not_found(self) -> None:
        """Test getting a non-existent skill returns None."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        skill = manager.get_skill_by_name("nonexistent-skill")

        assert skill is None

    def test_recommend_skills_returns_list(self) -> None:
        """Test that recommend_skills returns a list."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        result = manager.recommend_skills()

        assert isinstance(result, list)

    def test_recommend_skills_for_code_category(self) -> None:
        """Test that recommend_skills returns code-related skills."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        result = manager.recommend_skills(category="code")

        assert "gobby-tasks" in result

    def test_recommend_skills_for_docs_category(self) -> None:
        """Test that recommend_skills returns docs-related skills."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        result = manager.recommend_skills(category="docs")

        assert "gobby-tasks" in result
        assert "gobby-plan" in result

    def test_recommend_skills_unknown_category_returns_always_apply(self) -> None:
        """Test that recommend_skills returns alwaysApply skills for unknown category."""
        from gobby.hooks.skill_manager import HookSkillManager

        manager = HookSkillManager()
        result = manager.recommend_skills(category="unknown-category")

        # Should at least return some skills (alwaysApply ones)
        assert isinstance(result, list)


def _make_mock_skill(**overrides: object) -> MagicMock:
    """Build a mock Skill DB row with sensible defaults."""
    defaults = {
        "name": "test-skill",
        "description": "A test skill",
        "content": "# Test",
        "version": "1.0",
        "license": None,
        "compatibility": None,
        "allowed_tools": None,
        "metadata": None,
        "source_path": None,
        "source_type": "installed",
        "source_ref": None,
        "always_apply": False,
        "injection_format": "summary",
    }
    defaults.update(overrides)
    skill = MagicMock()
    for k, v in defaults.items():
        setattr(skill, k, v)
    return skill


@pytest.mark.unit
class TestDbSkillToParsed:
    """Tests for _db_skill_to_parsed audience_config reconstruction."""

    def test_reconstructs_audience_config(self) -> None:
        """audience_config is reconstructed from metadata.gobby."""
        from gobby.hooks.skill_manager import _db_skill_to_parsed

        skill = _make_mock_skill(metadata={"gobby": {"audience": "all", "triggers": ["test"]}})
        parsed = _db_skill_to_parsed(skill)

        assert parsed.audience_config is not None
        assert parsed.audience_config.audience == "all"

    def test_reconstructs_sources(self) -> None:
        """sources field round-trips through DB."""
        from gobby.hooks.skill_manager import _db_skill_to_parsed

        skill = _make_mock_skill(
            metadata={"gobby": {"sources": ["claude_sdk_web_chat", "gemini_sdk_web_chat"]}}
        )
        parsed = _db_skill_to_parsed(skill)

        assert parsed.audience_config is not None
        assert parsed.audience_config.sources == ["claude_sdk_web_chat", "gemini_sdk_web_chat"]

    def test_no_gobby_meta_returns_none(self) -> None:
        """audience_config is None when metadata lacks gobby key."""
        from gobby.hooks.skill_manager import _db_skill_to_parsed

        skill = _make_mock_skill(metadata={"author": "test"})
        parsed = _db_skill_to_parsed(skill)

        assert parsed.audience_config is None

    def test_full_audience_config(self) -> None:
        """All audience_config fields populate correctly."""
        from gobby.hooks.skill_manager import _db_skill_to_parsed

        skill = _make_mock_skill(
            metadata={
                "gobby": {
                    "audience": "autonomous",
                    "depth": 1,
                    "steps": ["plan", "execute"],
                    "task_categories": ["code", "test"],
                    "sources": ["claude-code"],
                    "format_overrides": {"autonomous": "full"},
                    "priority": 10,
                }
            }
        )
        parsed = _db_skill_to_parsed(skill)

        assert parsed.audience_config is not None
        assert parsed.audience_config.audience == "autonomous"
        assert parsed.audience_config.depth == 1
        assert parsed.audience_config.steps == ["plan", "execute"]
        assert parsed.audience_config.task_categories == ["code", "test"]
        assert parsed.audience_config.sources == ["claude-code"]
        assert parsed.audience_config.format_overrides == {"autonomous": "full"}
        assert parsed.audience_config.priority == 10

    def test_none_metadata_returns_none(self) -> None:
        """audience_config is None when metadata is None."""
        from gobby.hooks.skill_manager import _db_skill_to_parsed

        skill = _make_mock_skill(metadata=None)
        parsed = _db_skill_to_parsed(skill)

        assert parsed.audience_config is None
