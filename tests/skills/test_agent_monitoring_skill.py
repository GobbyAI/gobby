"""Content checks for the bundled agent-monitoring skill."""

from importlib.resources import files
from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path(str(files("gobby").joinpath("install/shared/skills/agent-monitoring/SKILL.md")))


class TestAgentMonitoringSkill:
    def test_skill_parses(self) -> None:
        parsed = parse_skill_file(SKILL_PATH)

        assert parsed.name == "agent-monitoring"
        assert parsed.description
        assert parsed.metadata is not None
        assert "gobby" in parsed.metadata

    def test_documents_supported_monitoring_tools(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")

        for text in (
            "gobby-agents",
            "list_running_agents",
            "get_running_agent",
            "list_agent_runs",
            "get_agent_result",
            "gobby-sessions",
            "get_session",
            "get_session_messages",
            "capture_output",
        ):
            assert text in body

    def test_documents_raw_fallbacks_as_debugging_only(self) -> None:
        body = SKILL_PATH.read_text(encoding="utf-8")

        assert "raw SQLite" in body
        assert "direct `tmux` commands" in body
        assert "debugging fallbacks" in body
