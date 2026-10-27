"""Tests for the Assistant and Orchestrator prompts naming the read-only query tool."""

from __future__ import annotations

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path

pytestmark = pytest.mark.unit

TOOL = "query_readonly"
STALE_HELPER = "read-only helper"


def _agent_prompt(name: str) -> str:
    data = yaml.safe_load((get_bundled_agents_path() / f"{name}.yaml").read_text())
    return str(data["prompts"]["agent"])


@pytest.mark.parametrize("name", ["assistant", "orchestrator"])
def test_seat_prompt_names_the_query_tool(name: str) -> None:
    assert TOOL in _agent_prompt(name)


def test_assistant_prompt_drops_the_stale_helper_reference() -> None:
    assert STALE_HELPER not in _agent_prompt("assistant")
