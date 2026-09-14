"""Tests for skill formatting helpers.

Relocated from tests/workflows/test_context_actions.py as part of dead-code cleanup.
"""

from unittest.mock import MagicMock, patch

import pytest

from gobby.skills.formatting import (
    SKILL_BLOCK_ATOMICITY_NOTICE,
    format_skill_block_reason,
    format_skill_fetch_context,
    recommend_skills_for_task,
    skill_fetch_batch_directive,
    skill_fetch_directive,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["python", "gobby:references/tasks/closing.md"])
def test_block_notice_precedes_specialized_guidance_once(name: str) -> None:
    directive = skill_fetch_directive(name)
    reason = f"{directive}\nThen inspect the staged changes."
    formatted = format_skill_block_reason(reason)
    assert formatted == f"{SKILL_BLOCK_ATOMICITY_NOTICE}\n{reason}"
    assert format_skill_block_reason(formatted) == formatted
    assert SKILL_BLOCK_ATOMICITY_NOTICE not in directive
    assert SKILL_BLOCK_ATOMICITY_NOTICE not in format_skill_fetch_context(name)
    assert SKILL_BLOCK_ATOMICITY_NOTICE not in skill_fetch_batch_directive([name, "restraint"])


def test_non_skill_block_keeps_its_reason() -> None:
    assert format_skill_block_reason("Claim a task first.") == "Claim a task first."


def test_block_notice_follows_leading_load_calls() -> None:
    header = "Rule enforced by Gobby: [step-enforcement:backend-developer/load_required_skills]"
    lead = 'get_skill(name="restraint")'
    detail = "Tool 'Bash' is not allowed in the 'load_required_skills' step."
    reason = f"{header}\n{lead}\n{detail}\n{skill_fetch_directive('restraint')}"

    assert format_skill_block_reason(reason) == (
        f"{header}\n{lead}\n{SKILL_BLOCK_ATOMICITY_NOTICE}\n{detail}\n"
        f"{skill_fetch_directive('restraint')}"
    )


def test_reference_contract_1_1_3() -> None:
    reference = "gobby:references/tasks/closing.md"
    directive = skill_fetch_batch_directive(["brevity", reference, reference])
    assert directive.count('call_tool("gobby-skills", "get_skill_file"') == 1
    assert directive.count('call_tool("gobby-skills", "get_skill", {"name":"brevity"})') == 1
    assert directive.index('"name":"brevity"') < directive.index("get_tool_schema")
    assert directive.index("get_tool_schema") < directive.index(
        'call_tool("gobby-skills", "get_skill_file"'
    )
    assert '"name":"gobby","path":"references/tasks/closing.md"' in directive
    assert "page.next_cursor" in directive
    assert "only cursor until null" in directive


class TestSkillFetchDirectives:
    def test_skill_fetch_directive_is_canonical(self) -> None:
        rendered = skill_fetch_directive("plan")

        assert rendered == (
            "Load and fully read the skill in its own outer tool result: "
            'call_tool("gobby-skills", "get_skill", {"name":"plan"}). Then continue.'
        )
        assert rendered.count("call_tool") == 1
        assert rendered.count("get_skill") == 1
        assert rendered.count("gobby-skills") == 1
        assert rendered.count("plan") == 1

    def test_format_skill_fetch_context_preserves_args(self) -> None:
        rendered = format_skill_fetch_context("plan", "draft auth flow")

        assert (
            "Load and fully read the skill in its own outer tool result: "
            'call_tool("gobby-skills", "get_skill", {"name":"plan"}). Then continue.'
        ) in rendered
        assert "User arguments: draft auth flow" in rendered

    def test_skill_fetch_batch_directive_calls_each_skill_directly(self) -> None:
        rendered = skill_fetch_batch_directive(
            ["loading-skills", "python", "python", "development-discipline"]
        )

        assert "list_mcp_servers" not in rendered
        assert "list_tools" not in rendered
        assert "get_tool_schema" not in rendered
        assert "in order" in rendered
        assert "one sequential tool call" in rendered
        assert "one separate outer tool result per skill" in rendered
        assert "Promise.all" in rendered
        assert "aggregate responses" in rendered
        assert rendered.count('call_tool("gobby-skills", "get_skill"') == 3
        assert rendered.index('{"name":"loading-skills"}') < rendered.index('{"name":"python"}')
        assert rendered.index('{"name":"python"}') < rendered.index(
            '{"name":"development-discipline"}'
        )
        assert rendered.rstrip().endswith("Then continue.")


class TestRecommendSkillsForTask:
    """Tests for the recommend_skills_for_task function."""

    @patch("gobby.hooks.skill_manager.HookSkillManager.recommend_skills")
    def test_returns_list(self, mock_recommend: MagicMock) -> None:
        """Should return a list of skill names."""
        task = {"title": "Test task"}
        mock_recommend.return_value = ["gobby-tasks"]
        result = recommend_skills_for_task(task)
        assert result == ["gobby-tasks"]
        mock_recommend.assert_called_once_with(category=None)

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

        assert result == ["gobby-tasks"]
        mock_recommend.assert_called_once_with(category=None)

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
        assert result == ["gobby-tasks"]
        mock_recommend.assert_called_once_with(category=None)
