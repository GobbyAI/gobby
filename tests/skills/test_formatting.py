"""Tests for skill formatting helpers (recommend_skills_for_task).

Relocated from tests/workflows/test_context_actions.py as part of dead-code cleanup.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.skills.formatting import (
    format_skill_fetch_context,
    recommend_skills_for_task,
    render_skills_for_context,
    skill_fetch_directive,
)

pytestmark = pytest.mark.unit


class TestRenderSkillsForContext:
    """Tests for render_skills_for_context manifests."""

    def test_summary_skills_render_as_active_manifest(self) -> None:
        skill = SimpleNamespace(
            name="plan-review",
            description="Review a gobby plan document.",
            content="# Full content",
        )

        rendered = render_skills_for_context([(skill, "summary")])

        assert "<active_skills>" in rendered
        assert "- name: plan-review" in rendered
        assert 'ref: gobby-skills:get_skill name="plan-review"' in rendered
        assert "# Full content" not in rendered

    def test_full_and_summary_skills_render_manifest_only(self) -> None:
        full_skill = SimpleNamespace(
            name="bridge",
            description="UI annotation workflow.",
            content="# Bridge content",
        )
        summary_skill = SimpleNamespace(
            name="brevity",
            description="Terse output mode.",
            content="# Brevity content",
        )

        rendered = render_skills_for_context([(full_skill, "full"), (summary_skill, "summary")])

        assert "- name: bridge" in rendered
        assert "- name: brevity" in rendered
        assert 'ref: gobby-skills:get_skill name="bridge"' in rendered
        assert 'ref: gobby-skills:get_skill name="brevity"' in rendered
        assert "# Bridge content" not in rendered
        assert "# Brevity content" not in rendered

    def test_deduplicates_manifest_entries(self) -> None:
        skill = SimpleNamespace(name="brevity", description="", content="body")

        rendered = render_skills_for_context([(skill, "full"), (skill, "summary")])

        assert rendered.count("- name: brevity") == 1
        assert "body" not in rendered


class TestSkillFetchDirectives:
    def test_skill_fetch_directive_is_canonical(self) -> None:
        assert (
            skill_fetch_directive("plan")
            == 'Call get_skill(name="plan") on gobby-skills, then continue.'
        )

    def test_format_skill_fetch_context_preserves_args(self) -> None:
        rendered = format_skill_fetch_context("plan", "draft auth flow")

        assert 'Call get_skill(name="plan") on gobby-skills, then continue.' in rendered
        assert "User arguments: draft auth flow" in rendered


class TestRecommendSkillsForTask:
    """Tests for the recommend_skills_for_task function."""

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_returns_list(self, mock_recommend: MagicMock) -> None:
        """Should return a list of skill names."""
        mock_recommend.return_value = ["gobby-tasks"]
        result = recommend_skills_for_task({"title": "Test task"})
        assert isinstance(result, list)

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_with_code_category(self, mock_recommend: MagicMock) -> None:
        """Should return code-related skills for code category."""
        mock_recommend.return_value = ["gobby-tasks"]
        task = {"title": "Test task", "category": "code"}
        result = recommend_skills_for_task(task)

        assert "gobby-tasks" in result

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_with_docs_category(self, mock_recommend: MagicMock) -> None:
        """Should return docs-related skills for docs category."""
        mock_recommend.return_value = ["gobby-tasks", "gobby-plan"]
        task = {"title": "Test task", "category": "docs"}
        result = recommend_skills_for_task(task)

        assert "gobby-tasks" in result
        assert "gobby-plan" in result

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_with_test_category(self, mock_recommend: MagicMock) -> None:
        """Should return test-related skills for test category."""
        mock_recommend.return_value = ["gobby-tasks"]
        task = {"title": "Test task", "category": "test"}
        result = recommend_skills_for_task(task)

        assert "gobby-tasks" in result

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_with_no_category(self, mock_recommend: MagicMock) -> None:
        """Should return always-apply skills when no category."""
        mock_recommend.return_value = ["gobby-tasks"]
        task = {"title": "Test task"}
        result = recommend_skills_for_task(task)

        assert isinstance(result, list)

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_with_none_task(self, mock_recommend: MagicMock) -> None:
        """Should return empty list for None task."""
        mock_recommend.return_value = []
        result = recommend_skills_for_task(None)
        assert result == []

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_with_empty_dict(self, mock_recommend: MagicMock) -> None:
        """Should return always-apply skills for empty dict."""
        mock_recommend.return_value = ["gobby-tasks"]
        result = recommend_skills_for_task({})
        assert isinstance(result, list)
