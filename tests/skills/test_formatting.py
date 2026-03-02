"""Tests for skill formatting helpers (recommend_skills_for_task).

Relocated from tests/workflows/test_context_actions.py as part of dead-code cleanup.
"""

from unittest.mock import MagicMock, patch

import pytest

from gobby.skills.formatting import recommend_skills_for_task

pytestmark = pytest.mark.unit


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
