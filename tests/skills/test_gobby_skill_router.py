"""Content-level tests for the bundled /gobby router skill."""

import importlib.resources
from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path(
    str(importlib.resources.files("gobby").joinpath("install/shared/skills/gobby/SKILL.md"))
)


class TestGobbyRouterSkill:
    def test_skill_parses(self) -> None:
        parsed = parse_skill_file(SKILL_PATH)

        assert parsed.name == "gobby"
        assert parsed.description
        assert "router" in parsed.description.lower()

    def test_documents_router_semantics(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")

        for text in (
            "/gobby` and `/gobby help",
            "/gobby <skill> [args]",
            "/gobby skill <skill> [args]",
            "/gobby:<skill> [args]",
            'get_skill(name="<skill>")',
            "does not inline skill bodies",
            "Preserve",
        ):
            assert text in body

    def test_references_dynamic_skill_and_mcp_discovery(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")

        for text in (
            "list_skills",
            "progressive discovery",
            "list_mcp_servers",
            "list_tools",
            "get_tool_schema",
            "call_tool",
            "arguments={...}",
        ):
            assert text in body

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
