"""Contract tests for the Gemini image-generation agent definition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = REPO_ROOT / "src/gobby/install/shared/workflows/agents/gemini-image-gen.yaml"
STRAY_WORKFLOW_PATH = REPO_ROOT / ".gobby/workflows/gemini-image-gen.yaml"


def _agent() -> dict[str, Any]:
    return yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))


def test_gemini_image_agent_lives_only_in_bundled_agents_dir() -> None:
    assert AGENT_PATH.exists()
    assert not STRAY_WORKFLOW_PATH.exists()


def test_gemini_image_agent_uses_image_generation_model() -> None:
    agent = _agent()

    assert agent["name"] == "gemini-image-gen"
    assert agent["enabled"] is True
    assert "spawn" in agent["surfaces"]
    assert agent["provider"] == "gemini"
    assert agent["model"] == "gemini-3-pro-image-preview"
    AgentDefinitionBody.model_validate(agent)


def test_gemini_image_agent_self_completes_with_end_agent_run() -> None:
    agent = _agent()
    terminate_step = next(
        (step for step in agent["steps"] if step["name"] == "terminate"),
        None,
    )

    assert terminate_step is not None
    assert "gobby-agents:end_agent_run" in terminate_step["allowed_mcp_tools"]
    assert "gobby-agents:kill_agent" not in terminate_step["allowed_mcp_tools"]
