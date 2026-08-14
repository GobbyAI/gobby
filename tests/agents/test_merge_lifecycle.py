"""Stage-native contracts for bundled merge agents."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit

LEGACY_MERGE_TOOLS = {
    "gobby-tasks:mark_task_merged",
    "gobby-tasks:mark_task_merge_failed",
    "gobby-tasks:mark_task_pr_opened",
    "gobby-tasks:advance_lifecycle",
}


def _agent(name: str) -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[2]
        / f"src/gobby/install/shared/workflows/agents/{name}.yaml"
    )
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _allowed_mcp_tools(agent: dict[str, Any]) -> set[str]:
    tools: set[str] = set()
    for step in agent.get("step_workflow", {}).get("steps", []):
        tools.update(step.get("allowed_mcp_tools", []) or [])
    return tools


def _step(agent: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [
        step for step in agent.get("step_workflow", {}).get("steps", []) if step.get("name") == name
    ]
    assert len(matches) == 1
    return cast(dict[str, Any], matches[0])


def _create_session(
    db: HubDatabase, session_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002"
) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (id) DO NOTHING",
        ("11111111-1111-4111-8111-111111110001", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW(), NOW()) ON CONFLICT (id) DO NOTHING",
        (
            session_id,
            "ext-1",
            "21000000-0000-4000-8000-000000000001",
            "claude",
            "11111111-1111-4111-8111-111111110001",
        ),
    )


def _install_merge_worker_workflow(
    db: HubDatabase,
    *,
    session_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
    current_step: str = "merge",
) -> AgentStepInstanceManager:
    agent = _agent("merge-worker")
    workflow_data = {
        "name": agent["name"],
        "version": agent["version"],
        "enabled": True,
        "variables": agent.get("step_workflow", {}).get("variables", {}),
        "steps": agent["step_workflow"]["steps"],
        "exit_condition": agent["step_workflow"].get("exit_condition"),
    }
    definition = WorkflowDefinition(**workflow_data)
    manager = AgentDefinitionManager(db)
    manager.create(
        name=definition.name,
        definition_json=json.dumps(workflow_data),
        priority=100,
        enabled=True,
    )

    _create_session(db, session_id)
    instance_manager = AgentStepInstanceManager(db)
    instance_manager.save(
        make_step_instance(
            session_id,
            agent_name=definition.name.removesuffix("-steps"),
            current_step=current_step,
            variables=dict(definition.variables),
        )
    )
    return instance_manager


def _mcp_event(
    mcp_key: str,
    *,
    event_type: HookEventType,
    arguments: dict[str, Any] | None = None,
    tool_output: dict[str, Any] | None = None,
    session_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
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
    if tool_output is not None:
        data["tool_output"] = tool_output
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"is_failure": is_error},
    )


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
async def test_merge_worker_allows_verify_before_recording_result(temp_db: HubDatabase) -> None:
    db = temp_db
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
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    assert response.decision == "allow"
    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "merge"


def test_merge_worker_allows_end_agent_run_only_in_terminate_step() -> None:
    agent = _agent("merge-worker")
    merge = _step(agent, "merge")
    terminate = _step(agent, "terminate")

    assert "gobby-agents:end_agent_run" not in merge["allowed_mcp_tools"]
    assert terminate["allowed_mcp_tools"] == ["gobby-agents:end_agent_run"]


def test_merge_worker_retries_worktree_cleanup_three_times_before_termination() -> None:
    agent = _agent("merge-worker")
    cleanup = _step(agent, "cleanup")

    assert agent["step_workflow"]["variables"]["worktree_cleanup_failures"] == 0
    failure_updates = [
        update
        for update in [*cleanup["on_mcp_success"], *cleanup["on_mcp_error"]]
        if update.get("variable") == "worktree_cleanup_failures"
    ]
    assert len(failure_updates) == 2
    assert all(
        update["value"] == "vars.get('worktree_cleanup_failures', 0) + 1"
        for update in failure_updates
    )
    assert {(transition["to"], transition["when"]) for transition in cleanup["transitions"]} >= {
        ("terminate", "vars.get('worktree_cleanup_failures', 0) >= 3")
    }


@pytest.mark.asyncio
async def test_merge_worker_failure_result_transitions_to_terminate(temp_db: HubDatabase) -> None:
    db = temp_db
    instance_manager = _install_merge_worker_workflow(db)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _mcp_event(
            "gobby-merge:merge_start",
            event_type=HookEventType.AFTER_TOOL,
            is_error=True,
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "merge"
    assert instance.variables["merge_worker_ready_to_terminate"] is False

    response = await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:record_merge_result",
            event_type=HookEventType.AFTER_TOOL,
            arguments={
                "task_id": "#14094",
                "failure_reason": "12 conflicts remained unresolved after retry cap",
            },
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "terminate"
    assert instance.variables["merge_result_recorded"] is True
    assert instance.variables["merge_worker_ready_to_terminate"] is True
    assert response.context is not None
    assert "merge -> terminate" in response.context


@pytest.mark.parametrize(
    ("mcp_key", "ready_to_terminate"),
    [
        ("gobby-tasks-ops:record_merge_result", False),
        ("gobby-tasks-ops:close_linked_github_issue", True),
    ],
)
async def test_merge_worker_tool_failure_without_durable_result_stays_in_merge(
    temp_db: HubDatabase,
    mcp_key: str,
    ready_to_terminate: bool,
) -> None:
    instance_manager = _install_merge_worker_workflow(temp_db)
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _mcp_event(mcp_key, event_type=HookEventType.AFTER_TOOL, is_error=True),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "merge"
    assert instance.variables["merge_result_recorded"] is False
    assert instance.variables["merge_worker_ready_to_terminate"] is ready_to_terminate


@pytest.mark.asyncio
async def test_merge_worker_success_waits_for_issue_close_then_cleanup(
    temp_db: HubDatabase,
) -> None:
    db = temp_db
    instance_manager = _install_merge_worker_workflow(db)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:record_merge_result",
            event_type=HookEventType.AFTER_TOOL,
            arguments={"task_id": "#14094", "merge_sha": "abc123"},
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )
    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "merge"
    assert instance.variables["merge_result_recorded"] is True

    await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:close_linked_github_issue",
            event_type=HookEventType.AFTER_TOOL,
            arguments={"task_id": "#14094", "merge_sha": "abc123"},
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "cleanup"

    await engine.evaluate(
        _mcp_event(
            "gobby-worktrees:delete_worktree",
            event_type=HookEventType.AFTER_TOOL,
            tool_output={"success": True},
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "terminate"


@pytest.mark.asyncio
async def test_merge_worker_issue_close_error_after_durable_result_enters_cleanup(
    temp_db: HubDatabase,
) -> None:
    instance_manager = _install_merge_worker_workflow(temp_db)
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:record_merge_result",
            event_type=HookEventType.AFTER_TOOL,
            arguments={"task_id": "#14094", "merge_sha": "abc123"},
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )
    await engine.evaluate(
        _mcp_event(
            "gobby-tasks-ops:close_linked_github_issue",
            event_type=HookEventType.AFTER_TOOL,
            is_error=True,
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "cleanup"
    assert instance.variables["merge_result_recorded"] is True


@pytest.mark.asyncio
async def test_merge_worker_blocks_premature_end_agent_run(temp_db: HubDatabase) -> None:
    db = temp_db
    _install_merge_worker_workflow(db)
    engine = RuleEngine(db)

    response = await engine.evaluate(
        _mcp_event("gobby-agents:end_agent_run", event_type=HookEventType.BEFORE_TOOL),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables={},
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "gobby-agents:end_agent_run" in response.reason
    assert "merge" in response.reason


@pytest.mark.asyncio
async def test_merge_worker_three_cleanup_failures_transition_to_terminate(
    temp_db: HubDatabase,
) -> None:
    instance_manager = _install_merge_worker_workflow(temp_db, current_step="cleanup")
    engine = RuleEngine(temp_db)
    variables: dict[str, Any] = {}

    failure_events = [
        _mcp_event(
            "gobby-worktrees:delete_worktree",
            event_type=HookEventType.AFTER_TOOL,
            is_error=True,
        ),
        _mcp_event(
            "gobby-worktrees:delete_worktree",
            event_type=HookEventType.AFTER_TOOL,
            tool_output={"success": False},
        ),
    ]
    for expected_failures, event in enumerate(failure_events, start=1):
        await engine.evaluate(
            event,
            session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
            variables=variables,
        )
        instance = instance_manager.get_for_session(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002"
        )
        assert instance is not None
        assert instance.current_step == "cleanup"
        assert instance.variables["worktree_cleanup_failures"] == expected_failures

    await engine.evaluate(
        _mcp_event(
            "gobby-worktrees:delete_worktree",
            event_type=HookEventType.AFTER_TOOL,
            is_error=True,
        ),
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002",
        variables=variables,
    )

    instance = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa3002")
    assert instance is not None
    assert instance.current_step == "terminate"
    assert instance.variables["worktree_cleanup_failures"] == 3
    assert instance.variables.get("worktree_cleanup_done") is not True
