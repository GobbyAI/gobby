"""Contract tests for the bundled communications coordinator persona."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.native_tools import is_known_native_tool

pytestmark = pytest.mark.unit

_DEFINITION_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "workflows"
    / "agents"
    / "comms-agent.yaml"
)


def test_comms_agent_is_a_restricted_persona_coordinator() -> None:
    raw = yaml.safe_load(_DEFINITION_PATH.read_text())
    agent = AgentDefinitionBody.model_validate(raw)

    assert agent.name == "comms-agent"
    assert agent.surfaces == ["persona"]
    assert agent.provider == "inherit"
    assert {
        "Bash",
        "Edit",
        "NotebookEdit",
        "Write",
        "apply_patch",
        "run_shell_command",
        "shell",
        "write_file",
    } <= set(agent.blocked_tools)
    assert agent.prompts.persona is not None
    assert "gobby-agents" in agent.prompts.persona
    assert "gobby-communications:set_channel_project" in agent.prompts.persona
    assert all(is_known_native_tool(tool) for tool in agent.blocked_tools)
