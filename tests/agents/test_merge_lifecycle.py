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


def _step(agent: dict, name: str) -> dict:
    matches = [step for step in agent.get("steps", []) if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def test_merge_orchestrator_uses_stage_native_merge_result_tool() -> None:
    tools = _allowed_mcp_tools(_agent("merge-orchestrator"))

    assert "gobby-tasks-ops:record_merge_result" in tools
    assert tools.isdisjoint(LEGACY_MERGE_TOOLS)


def test_merge_worker_uses_stage_native_merge_result_tool() -> None:
    tools = _allowed_mcp_tools(_agent("merge-worker"))

    assert "gobby-tasks-ops:record_merge_result" in tools
    assert tools.isdisjoint(LEGACY_MERGE_TOOLS)


def test_merge_worker_default_provider_is_reliable_for_unattended_merges() -> None:
    agent = _agent("merge-worker")

    assert agent["provider"] == "claude"
    assert agent["model"] == "sonnet"


def test_merge_orchestrator_instructions_do_not_reference_removed_lifecycle_tools() -> None:
    text = Path("src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml").read_text(
        encoding="utf-8"
    )

    for tool in LEGACY_MERGE_TOOLS:
        assert tool.split(":", 1)[1] not in text


def test_merge_worker_retry_cap_tracks_conflict_ids_not_total_calls() -> None:
    merge = _step(_agent("merge-worker"), "merge")
    before_handlers = merge["on_mcp_before"]
    block_handler = next(handler for handler in before_handlers if handler["action"] == "block")
    set_handler = next(
        handler for handler in before_handlers if handler["action"] == "set_variable"
    )

    assert "merge_retry_count" not in str(merge)
    assert set_handler["variable"] == "merge_resolve_attempts"
    assert ".count(tool_input.get('conflict_id'" in block_handler["when"]
    assert "[tool_input.get('conflict_id'" in set_handler["value"]


def test_merge_worker_retry_cap_guidance_continues_remaining_conflicts() -> None:
    agent = _agent("merge-worker")
    merge = _step(agent, "merge")
    block_handler = next(
        handler for handler in merge["on_mcp_before"] if handler["action"] == "block"
    )
    workflow_text = str(agent)
    retry_reason = block_handler["reason"]

    assert "skip that conflict_id and continue" in workflow_text
    assert "Do not call merge_abort solely because one conflict_id" in workflow_text
    assert "Call gobby-merge:merge_abort" not in retry_reason
    assert "Skip it, continue with the next pending conflict_id" in retry_reason
    assert "Do not call merge_abort" in retry_reason
    assert "solely because one conflict_id" in retry_reason
