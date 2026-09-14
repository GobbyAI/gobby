"""Content-level tests for the bundled /gobby router skill."""

from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.event_handlers._agent import AgentEventHandlerMixin
from gobby.hooks.skill_manager import HookSkillManager
from gobby.skills.parser import ParsedSkill, parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path(str(files("gobby").joinpath("install/shared/skills/gobby/SKILL.md")))


def test_reference_contract_3_1_2() -> None:
    """Provider interception keeps project resolution and multiline level arguments."""
    override = ParsedSkill(
        name="brevity",
        description="Project brevity",
        content="PROJECT BODY",
        metadata={"gobby": {"levels": ["normal", "max"]}},
    )
    handler = AgentEventHandlerMixin()
    handler._skill_manager = HookSkillManager()
    with patch.object(
        handler._skill_manager, "discover_core_skills", return_value=[override]
    ) as discover:
        result = handler._intercept_skill_command(
            "$gobby skill brevity max\nretain evidence", project_id="project-override"
        )
    discover.assert_called_once_with("project-override")
    assert result is not None
    assert '"name": "brevity"' in result
    assert '"level": "max"' in result
    assert "retain evidence" not in result
    assert "PROJECT BODY" not in result


class TestGobbyRouterSkill:
    def test_skill_parses(self) -> None:
        parsed = parse_skill_file(SKILL_PATH)

        assert parsed.name == "gobby"
        assert parsed.description
        assert "router" in parsed.description.lower()

    def test_documents_router_semantics(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")

        for text in (
            "$gobby help",
            "/gobby help",
            "$gobby <skill>",
            "/gobby <skill> [args]",
            "$gobby skill <skill> [args]",
            "/gobby skill <skill> [args]",
            "/gobby:<skill> [args]",
            'get_skill(name="<skill>")',
            "does not inline skill bodies",
            "Trailing command arguments remain in the original user prompt",
            "must not be",
            "duplicated into `<gobby-context>`",
            "Every displayed command uses the active provider's prefix",
        ):
            assert text in body

    def test_help_finishes_without_discovery(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")
        help_section = body.split("## Help Requests", 1)[1].split("## Explicit Loading", 1)[0]
        assert "Make zero tool calls" in help_section
        assert "daemon-supplied help menu immediately and finish" in help_section
        assert "Gobby help is unavailable." in help_section
        for tool in ("list_skills", "get_skill_file", "get_tool_schema", "list_mcp_servers"):
            assert tool not in help_section
        assert 'get_skill_file(name="gobby", path="catalog.json")' in body
        assert "This discovery applies only to explicit loading requests" in body

    def test_does_not_reintroduce_hard_coded_shortcuts(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")

        for stale_shortcut in (
            "/gobby tasks",
            "/gobby expand",
            "/gobby plan",
            "/gobby memory",
            "/gobby sessions",
            "/gobby worktrees",
            "/gobby merge",
            "/gobby agents",
            "/gobby doctor",
            "/gobby commit",
            "source-control",
        ):
            assert stale_shortcut not in body
