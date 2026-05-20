"""Contracts for merge-orchestrator step tooling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = REPO_ROOT / "src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml"
SKILL_PATH = REPO_ROOT / "src/gobby/install/shared/skills/merge-expert/SKILL.md"

REQUIRED_EXECUTE_TOOLS = {
    "gobby-tasks-ops:get_delivery_state",
    "gobby-tasks-ops:record_pr_state",
    "gobby-tasks-ops:record_pr_opened",
    "gobby-tasks-ops:open_delivery_pr",
    "gobby-tasks-ops:submit_for_review",
    "gobby-tasks-ops:record_pr_verdict",
    "gobby-tasks-ops:record_merge_result",
    "gobby-tasks-ops:close_linked_github_issue",
    "gobby-tasks-ops:append_description_section",
    "gobby-tasks:escalate_task",
    "gobby-worktrees:push_branch",
    "gobby-worktrees:merge_worktree",
    "gobby-worktrees:list_worktrees",
    "gobby-merge:analyze_merge_landscape",
    "gobby-merge:predict_conflicts",
    "gobby-merge:inspect_merge_state",
    "gobby-merge:merge_start",
    "gobby-merge:merge_status",
    "gobby-merge:merge_resolve",
    "gobby-merge:merge_apply",
    "gobby-merge:merge_abort",
    "gobby-merge:cherry_pick_into_worktree",
    "gobby-merge:merge_subset",
    "gobby-merge:verify_in_worktree",
    "gobby-agents:can_spawn_agent",
    "gobby-agents:evaluate_spawn",
    "gobby-agents:spawn_agent",
    "gobby-agents:dispatch_batch",
    "gobby-agents:wait_for_agent",
    "gobby-agents:list_agent_runs",
    "gobby-agents:get_agent_result",
    "gobby-agents:list_running_agents",
    "gobby-agents:get_running_agent",
    "gobby-agents:running_agent_stats",
    "github:create_pull_request_review",
    "github:get_pull_request",
    "github:get_pull_request_status",
    "github:get_pull_request_reviews",
    "github:update_pull_request_branch",
    "github:merge_pull_request",
}

FORBIDDEN_EXECUTE_TOOLS = {
    "gobby-tasks:close_task",
    "gobby-tasks:reopen_task",
    "gobby-agents:kill_agent",
}


@pytest.fixture
def db(tmp_path: Path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "test_merge_orchestrator_contract.db")
    run_migrations(database)
    return database


def _agent() -> dict[str, Any]:
    data = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _step(agent: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in agent["steps"] if step["name"] == name]
    assert len(matches) == 1
    return matches[0]


def _create_session(db: LocalDatabase, session_id: str = "agent-session") -> None:
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


def _install_workflow(db: LocalDatabase, *, current_step: str) -> WorkflowInstanceManager:
    agent = _agent()
    _create_session(db)
    definition = {
        "name": agent["name"],
        "version": agent["version"],
        "enabled": True,
        "variables": agent["step_variables"],
        "steps": agent["steps"],
        "exit_condition": agent["exit_condition"],
    }
    LocalWorkflowDefinitionManager(db).create(
        name=agent["name"],
        definition_json=json.dumps(definition),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )
    manager = WorkflowInstanceManager(db)
    manager.save_instance(
        WorkflowInstance(
            id="inst-agent-session-merge-orchestrator",
            session_id="agent-session",
            workflow_name=agent["name"],
            enabled=True,
            priority=100,
            current_step=current_step,
            step_entered_at=datetime.now(UTC),
            variables=dict(agent["step_variables"]),
        )
    )
    return manager


def _before_mcp_tool(mcp_key: str) -> HookEvent:
    server, tool = mcp_key.split(":", 1)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="agent-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": server, "tool_name": tool},
        },
        metadata={},
    )


def _after_mcp_tool(
    mcp_key: str,
    *,
    arguments: dict[str, Any] | None = None,
    tool_output: dict[str, Any] | None = None,
) -> HookEvent:
    server, tool = mcp_key.split(":", 1)
    tool_input: dict[str, Any] = {"server_name": server, "tool_name": tool}
    if arguments is not None:
        tool_input["arguments"] = arguments
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="agent-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": tool_input,
            "tool_output": tool_output or {"success": True},
        },
        metadata={},
    )


def _after_set_variable(name: str, value: object) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="agent-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": {"name": name, "value": value, "session_id": "#1"},
            "tool_output": {"success": True, "value": value, "scope": "session"},
        },
        metadata={},
    )


def test_execute_allow_list_matches_merge_expert_contract() -> None:
    execute = _step(_agent(), "execute")
    allowed = set(execute["allowed_mcp_tools"])
    blocked = set(execute["blocked_mcp_tools"])
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert REQUIRED_EXECUTE_TOOLS <= allowed
    assert FORBIDDEN_EXECUTE_TOOLS <= blocked
    assert "gobby-agents:kill_agent" not in allowed
    assert "Workers terminate themselves via `end_agent_run`" in skill
    assert "gobby-agents:wait_for_agent" in skill
    assert "Do not use Bash sleep loops" in skill
    assert "Workers terminate themselves via `kill_agent`" not in skill


def test_merge_orchestrator_uses_bounded_agent_waits() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    execute = _step(agent, "execute")

    assert "gobby-agents:wait_for_agent" in instructions
    assert "Do NOT use Bash sleep loops" in instructions
    assert "provider Monitor" in instructions
    assert "gobby-agents:wait_for_agent" in execute["allowed_mcp_tools"]
    assert "worker returns success" in instructions
    assert "unresolved merge state" in instructions
    assert "resolved_count/pending_count signature is unchanged" in instructions
    assert "scoped to workers this orchestrator run actually" in instructions
    assert "waited for" in instructions
    assert "historical delivery campaign failures" in instructions
    assert "continue the active resolution" in instructions
    assert "no_progress_merge_status_count" in agent["step_variables"]


@pytest.mark.asyncio
async def test_execute_step_allows_contract_tools_and_blocks_lifecycle_hazards(
    db: LocalDatabase,
) -> None:
    _install_workflow(db, current_step="execute")
    engine = RuleEngine(db)

    for mcp_key in sorted(REQUIRED_EXECUTE_TOOLS):
        response = await engine.evaluate(
            _before_mcp_tool(mcp_key),
            session_id="agent-session",
            variables={},
        )
        assert response.decision == "allow", mcp_key

    for mcp_key in sorted(FORBIDDEN_EXECUTE_TOOLS):
        response = await engine.evaluate(
            _before_mcp_tool(mcp_key),
            session_id="agent-session",
            variables={},
        )
        assert response.decision == "block", mcp_key


@pytest.mark.asyncio
async def test_merge_orchestrator_survey_plan_execute_report_path(db: LocalDatabase) -> None:
    manager = _install_workflow(db, current_step="survey")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _after_mcp_tool("gobby-merge:predict_conflicts"),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "plan"

    await engine.evaluate(
        _after_set_variable("merge_plan", [{"step_no": 1, "worktree_id": "wt-1"}]),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "execute"

    for mcp_key in ("gobby-merge:inspect_merge_state", "gobby-agents:spawn_agent"):
        response = await engine.evaluate(
            _before_mcp_tool(mcp_key),
            session_id="agent-session",
            variables=variables,
        )
        assert response.decision == "allow", mcp_key

    await engine.evaluate(
        _after_mcp_tool("gobby-merge:verify_in_worktree", arguments={"final": True}),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "report"


@pytest.mark.asyncio
async def test_execute_failure_report_can_record_merge_result_and_terminate(
    db: LocalDatabase,
) -> None:
    manager = _install_workflow(db, current_step="execute")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _before_mcp_tool("gobby-tasks-ops:record_merge_result"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "allow"

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-tasks-ops:record_merge_result",
            arguments={
                "task_id": "#14094",
                "failure_reason": (
                    "worker run-66394ffe69ab exited success but merge_status still "
                    "showed 17 pending conflicts"
                ),
            },
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "terminate"
    assert instance.variables["execution_complete"] is True
    assert instance.variables["report_complete"] is True


@pytest.mark.asyncio
async def test_execute_blocks_no_progress_worker_redispatch(db: LocalDatabase) -> None:
    manager = _install_workflow(db, current_step="execute")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-merge:merge_status",
            arguments={"resolution_id": "mr-1"},
            tool_output={"success": True, "result": {"resolved_count": 5, "pending_count": 12}},
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.variables["last_merge_status_signature"] == "mr-1:5/12"
    assert instance.variables["no_progress_merge_status_count"] == 0

    response = await engine.evaluate(
        _before_mcp_tool("gobby-agents:spawn_agent"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "allow"

    await engine.evaluate(
        _after_mcp_tool("gobby-agents:spawn_agent"),
        session_id="agent-session",
        variables=variables,
    )
    await engine.evaluate(
        _after_mcp_tool(
            "gobby-agents:wait_for_agent",
            tool_output={
                "success": True,
                "result": {
                    "completed": True,
                    "run_id": "run-worker",
                    "status": "success",
                    "result": "",
                },
            },
        ),
        session_id="agent-session",
        variables=variables,
    )

    response = await engine.evaluate(
        _before_mcp_tool("gobby-agents:spawn_agent"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "block"
    assert "merge_status has not been checked" in (response.reason or "")

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-merge:merge_status",
            arguments={"resolution_id": "mr-1"},
            tool_output={"success": True, "result": {"resolved_count": 5, "pending_count": 12}},
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.variables["post_worker_merge_status_checked"] is True
    assert instance.variables["no_progress_merge_status_count"] == 1
    assert instance.variables["last_worker_run_id"] == "run-worker"

    response = await engine.evaluate(
        _before_mcp_tool("gobby-agents:spawn_agent"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "block"

    response = await engine.evaluate(
        _before_mcp_tool("gobby-tasks-ops:record_merge_result"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_execute_allows_fresh_dispatch_with_historical_no_progress_state(
    db: LocalDatabase,
) -> None:
    manager = _install_workflow(db, current_step="execute")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-merge:merge_status",
            arguments={"resolution_id": "mr-1"},
            tool_output={"success": True, "result": {"resolved_count": 5, "pending_count": 12}},
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.variables["last_merge_status_signature"] == "mr-1:5/12"
    assert instance.variables["no_progress_merge_status_count"] == 0
    assert instance.variables["merge_worker_completed"] is False

    response = await engine.evaluate(
        _before_mcp_tool("gobby-agents:spawn_agent"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "allow"


def test_merge_expert_continues_active_resolution_before_abort() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert "active_resolution_id" in skill
    assert "continue that active" in skill
    assert "do not abort solely" in skill
    assert "previous campaign recorded no progress" in skill
    assert "no-progress redispatch cap applies" in skill
    assert "current orchestrator run completes" in skill


def test_merge_orchestrator_filters_running_workers_by_agent_identity() -> None:
    instructions = _agent()["instructions"]

    assert "agent_name is exactly `merge-worker`" in instructions
    assert "Never wait on this orchestrator's" in instructions
    assert "never treat another `merge-orchestrator` run as a" in instructions


def test_merge_orchestrator_worker_prompts_stay_inside_merge_tool_surface() -> None:
    instructions = " ".join(_agent()["instructions"].split())
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "For ordinary clean worktree delivery" in instructions
    assert "gobby-worktrees:merge_worktree" in instructions
    assert "run verify_in_worktree before recording success" in instructions
    assert "record the merge_sha returned by merge_worktree" in instructions
    assert "Do not instruct a clean worker to use merge_start/merge_apply" in instructions
    assert "merge_resolve(conflict_id=..., use_ai=true)" in instructions
    assert "exactly one pending conflict_id at a time" in instructions
    assert "multiple merge_resolve calls in the same assistant turn" in instructions
    assert "Do not" in instructions
    assert "synthesize manual" in instructions
    assert "merge_resolve(conflict_id=..., use_ai=true)" in skill
    assert "exactly one pending conflict_id at a time" in skill
    assert "multiple `merge_resolve` calls in the same assistant turn" in skill
    assert "Do not" in skill
    assert "synthesize manual `resolved_content`" in skill
    assert "Prefer worker-side verification" in skill
