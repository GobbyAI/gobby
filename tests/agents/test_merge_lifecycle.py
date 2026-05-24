"""Stage-native contracts for bundled merge agents."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition, WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager
from tests.fixtures.migrations import run_migrations

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


def _create_session(db: HubDatabase, session_id: str = "merge-worker-session") -> None:
    db.execute(
        "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, datetime('now'))",
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        (session_id, "ext-1", "machine-1", "claude", "project-1"),
    )


def _install_merge_worker_workflow(
    db: HubDatabase,
    *,
    session_id: str = "merge-worker-session",
    current_step: str = "merge",
) -> WorkflowInstanceManager:
    agent = _agent("merge-worker")
    workflow_data = {
        "name": agent["name"],
        "version": agent["version"],
        "enabled": True,
        "variables": agent.get("step_variables", {}),
        "steps": agent["steps"],
        "exit_condition": agent["exit_condition"],
    }
    definition = WorkflowDefinition(**workflow_data)
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name=definition.name,
        definition_json=json.dumps(workflow_data),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )

    _create_session(db, session_id)
    instance_manager = WorkflowInstanceManager(db)
    instance_manager.save_instance(
        WorkflowInstance(
            id=f"inst-{session_id}-{definition.name}",
            session_id=session_id,
            workflow_name=definition.name,
            enabled=True,
            priority=100,
            current_step=current_step,
            step_entered_at=datetime.now(UTC),
            variables=dict(definition.variables),
        )
    )
    return instance_manager


def _mcp_event(
    mcp_key: str,
    *,
    event_type: HookEventType,
    arguments: dict[str, Any] | None = None,
    session_id: str = "merge-worker-session",
    is_error: bool = False,
) -> HookEvent:
    server, tool = mcp_key.split(":", 1)
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": server,
            "tool_name": tool,
            "arguments": arguments or {},
        },
    }
    if is_error:
        data["is_error"] = True
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"is_failure": is_error},
    )


def _test_db(tmp_path: Path) -> HubDatabase:
    db = HubDatabase(tmp_path / "merge-worker.db")
    run_migrations(db)
    return db


def test_merge_orchestrator_uses_stage_native_merge_result_tool() -> None:
    tools = _allowed_mcp_tools(_agent("merge-orchestrator"))

    assert "gobby-tasks-ops:record_merge_result" in tools
    assert tools.isdisjoint(LEGACY_MERGE_TOOLS)


def test_merge_worker_uses_stage_native_merge_result_tool() -> None:
    tools = _allowed_mcp_tools(_agent("merge-worker"))

    assert "gobby-tasks-ops:record_merge_result" in tools
    assert tools.isdisjoint(LEGACY_MERGE_TOOLS)


def test_merge_worker_allows_read_only_preflight_tools_without_prompting_them() -> None:
    merge = _step(_agent("merge-worker"), "merge")
    tools = set(merge["allowed_mcp_tools"])
    text = " ".join(
        [
            _agent("merge-worker")["instructions"],
            merge["status_message"],
        ]
    )

    assert "inspect_merge_state" not in text
    assert "get_worktree_by_task" not in text
    assert "gobby-merge:inspect_merge_state" in tools
    assert "gobby-worktrees:get_worktree" in tools
    assert "gobby-worktrees:get_worktree_by_task" in tools


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
    success_handlers = merge["on_mcp_success"]
    error_handlers = merge["on_mcp_error"]
    block_handler = next(handler for handler in before_handlers if handler["action"] == "block")
    set_handler = next(
        handler
        for handler in success_handlers
        if handler["tool"] == "merge_resolve" and handler["action"] == "set_variable"
    )
    retry_later_handler = next(
        handler
        for handler in error_handlers
        if handler["tool"] == "merge_resolve" and handler["action"] == "set_variable"
    )

    assert "merge_retry_count" not in str(merge)
    assert set_handler["variable"] == "merge_resolve_attempts"
    assert retry_later_handler["variable"] == "merge_resolve_attempts"
    assert "retry_later" in retry_later_handler["when"]
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


def test_merge_worker_resolves_conflicts_sequentially() -> None:
    agent = _agent("merge-worker")
    workflow_text = " ".join(str(agent).split())
    merge_status = " ".join(_step(agent, "merge")["status_message"].split())

    assert "Resolve conflicts sequentially" in workflow_text
    assert "exactly one conflict_id at a time" in workflow_text
    assert "Never issue multiple" in workflow_text
    assert "merge_resolve calls" in workflow_text
    assert "Do NOT parallelize conflict resolution" in workflow_text
    assert "call merge_status before selecting the next pending conflict" in merge_status


def test_merge_worker_guidance_stays_inside_merge_tool_surface() -> None:
    agent = _agent("merge-worker")
    instructions = agent["instructions"]
    normalized_instructions = " ".join(instructions.split())
    merge_status = " ".join(_step(agent, "merge")["status_message"].split())

    assert "Do NOT use Bash, Read, or other file-inspection tools" in instructions
    assert "merge_worktree is the authoritative final landing tool" in instructions
    assert "do not record its SHA as the final delivery SHA" in merge_status
    assert "merge_resolve(use_ai=true)" in instructions
    assert "Do not switch to Bash/Read" in instructions
    assert "Do not call Read, Bash, or file-inspection tools" in merge_status
    assert "manual resolved_content" in merge_status
    assert "verification is a pre-record success gate" in normalized_instructions
    assert "call verify_in_worktree before record_merge_result" in merge_status


def test_merge_worker_preserves_guarded_verification_commands() -> None:
    agent = _agent("merge-worker")
    text = " ".join(
        [
            agent["instructions"],
            _step(agent, "merge")["status_message"],
        ]
    )

    assert "Pass verification commands exactly as supplied" in text
    assert "GOBBY_TEST_PROTECT=1 uv run pytest" in text
    assert "env GOBBY_TEST_PROTECT=1 uv run pytest" in text
    assert "retry pytest without them" in text


@pytest.mark.asyncio
async def test_merge_worker_allows_verify_before_recording_result(tmp_path: Path) -> None:
    db = _test_db(tmp_path)
    instance_manager = _install_merge_worker_workflow(db)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _mcp_event(
            "gobby-merge:verify_in_worktree",
            event_type=HookEventType.BEFORE_TOOL,
            arguments={
                "worktree_id": "wt-1",
                "command": "uv run pytest tests/cli/test_postgres_cli.py -v",
            },
        ),
        session_id="merge-worker-session",
        variables=variables,
    )

    assert response.decision == "allow"
    instance = instance_manager.get_instance("merge-worker-session", "merge-worker")
    assert instance is not None
    assert instance.current_step == "merge"


def test_merge_worker_allows_end_agent_run_only_in_terminate_step() -> None:
    agent = _agent("merge-worker")
    merge = _step(agent, "merge")
    terminate = _step(agent, "terminate")

    assert "gobby-agents:end_agent_run" not in merge["allowed_mcp_tools"]
    assert terminate["allowed_mcp_tools"] == ["gobby-agents:end_agent_run"]


@pytest.mark.asyncio
async def test_merge_worker_failure_result_transitions_to_terminate(tmp_path: Path) -> None:
    db = _test_db(tmp_path)
    instance_manager = _install_merge_worker_workflow(db)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:record_merge_result",
            event_type=HookEventType.AFTER_TOOL,
            arguments={
                "task_id": "#14094",
                "failure_reason": "12 conflicts remained unresolved after retry cap",
            },
        ),
        session_id="merge-worker-session",
        variables=variables,
    )

    instance = instance_manager.get_instance("merge-worker-session", "merge-worker")
    assert instance is not None
    assert instance.current_step == "terminate"
    assert instance.variables["merge_worker_ready_to_terminate"] is True
    assert response.context is not None
    assert "merge -> terminate" in response.context


@pytest.mark.asyncio
async def test_merge_worker_success_waits_for_issue_close_before_terminate(
    tmp_path: Path,
) -> None:
    db = _test_db(tmp_path)
    instance_manager = _install_merge_worker_workflow(db)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:record_merge_result",
            event_type=HookEventType.AFTER_TOOL,
            arguments={"task_id": "#14094", "merge_sha": "abc123"},
        ),
        session_id="merge-worker-session",
        variables=variables,
    )
    instance = instance_manager.get_instance("merge-worker-session", "merge-worker")
    assert instance is not None
    assert instance.current_step == "merge"
    assert instance.variables["merge_result_recorded"] is True

    await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:close_linked_github_issue",
            event_type=HookEventType.AFTER_TOOL,
            arguments={"task_id": "#14094", "merge_sha": "abc123"},
        ),
        session_id="merge-worker-session",
        variables=variables,
    )

    instance = instance_manager.get_instance("merge-worker-session", "merge-worker")
    assert instance is not None
    assert instance.current_step == "terminate"


@pytest.mark.asyncio
async def test_merge_worker_blocks_premature_end_agent_run(tmp_path: Path) -> None:
    db = _test_db(tmp_path)
    _install_merge_worker_workflow(db)
    engine = RuleEngine(db)

    response = await engine.evaluate(
        _mcp_event("gobby-agents:end_agent_run", event_type=HookEventType.BEFORE_TOOL),
        session_id="merge-worker-session",
        variables={},
    )

    assert response.decision == "block"
    assert "gobby-agents:end_agent_run" in response.reason
    assert "merge" in response.reason
