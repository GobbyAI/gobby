"""Stage-native contracts for bundled merge agents."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

LEGACY_MERGE_TOOLS = {
    "gobby-tasks:mark_task_merged",
    "gobby-tasks:mark_task_merge_failed",
    "gobby-tasks:mark_task_pr_opened",
    "gobby-tasks:advance_lifecycle",
}


def _agent(name: str) -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / f"src/gobby/install/shared/workflows/agents/{name}.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _allowed_mcp_tools(agent: dict) -> set[str]:
    tools: set[str] = set()
    for step in agent.get("steps", []):
        tools.update(step.get("allowed_mcp_tools", []) or [])
    return tools


def test_merge_orchestrator_uses_stage_native_merge_result_tool() -> None:
    tools = _allowed_mcp_tools(_agent("merge-orchestrator"))

    assert "gobby-tasks-ops:record_merge_result" in tools
    assert tools.isdisjoint(LEGACY_MERGE_TOOLS)


def test_merge_worker_uses_stage_native_merge_result_tool() -> None:
    tools = _allowed_mcp_tools(_agent("merge-worker"))

    assert "gobby-tasks-ops:record_merge_result" in tools
    assert tools.isdisjoint(LEGACY_MERGE_TOOLS)


def test_merge_orchestrator_instructions_do_not_reference_removed_lifecycle_tools() -> None:
    text = Path("src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml").read_text(
        encoding="utf-8"
    )

    for tool in LEGACY_MERGE_TOOLS:
        assert tool.split(":", 1)[1] not in text
