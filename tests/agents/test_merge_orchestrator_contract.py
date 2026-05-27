"""Contracts for merge-orchestrator step tooling."""

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
    "gobby-tasks:close_task",
    "gobby-tasks:escalate_task",
    "gobby-skills:get_skill",
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
    "gobby-tasks:reopen_task",
    "gobby-agents:kill_agent",
}


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    _create_contract_schema(temp_db)
    return temp_db


def _create_contract_schema(db: HubDatabase) -> None:
    """Create the narrow workflow schema this contract test exercises."""
    if str(getattr(db, "dialect", "")).startswith("postgres"):
        row = db.fetchone(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema = current_schema()
               AND table_name = 'projects'
            """
        )
        if row is not None:
            return
    statements = [
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            source TEXT NOT NULL,
            project_id TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE workflow_definitions (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            name TEXT NOT NULL,
            description TEXT,
            workflow_type TEXT NOT NULL DEFAULT 'workflow',
            version TEXT DEFAULT '1.0',
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 100,
            sources TEXT,
            definition_json TEXT NOT NULL,
            canvas_json TEXT,
            source TEXT DEFAULT 'installed',
            tags TEXT,
            deleted_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE workflow_instances (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workflow_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 100,
            current_step TEXT,
            step_entered_at TEXT,
            step_action_count INTEGER DEFAULT 0,
            total_action_count INTEGER DEFAULT 0,
            variables TEXT DEFAULT '{}',
            context_injected INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(session_id, workflow_name)
        )
        """,
        """
        CREATE TABLE session_variables (
            session_id TEXT PRIMARY KEY,
            variables TEXT DEFAULT '{}',
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE workflow_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            step TEXT NOT NULL,
            event_type TEXT NOT NULL,
            tool_name TEXT,
            rule_id TEXT,
            condition TEXT,
            result TEXT NOT NULL,
            reason TEXT,
            context TEXT
        )
        """,
        """
        CREATE TABLE config_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            is_secret INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
        """,
        """
        CREATE TABLE rule_overrides (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(session_id, rule_name)
        )
        """,
    ]
    for statement in statements:
        db.execute(statement)


def _agent() -> dict[str, Any]:
    data = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _step(agent: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in agent["steps"] if step["name"] == name]
    assert len(matches) == 1
    return matches[0]


def _create_session(db: HubDatabase, session_id: str = "agent-session") -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (id) DO NOTHING",
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW(), NOW()) ON CONFLICT (id) DO NOTHING",
        (session_id, f"ext-{session_id}", "machine-1", "claude", "project-1"),
    )


def _install_workflow(
    db: HubDatabase,
    *,
    current_step: str,
    session_id: str = "agent-session",
) -> WorkflowInstanceManager:
    agent = _agent()
    _create_session(db, session_id=session_id)
    definition = {
        "name": agent["name"],
        "version": agent["version"],
        "enabled": True,
        "variables": agent["step_variables"],
        "steps": agent["steps"],
        "exit_condition": agent["exit_condition"],
    }
    definition_manager = LocalWorkflowDefinitionManager(db)
    existing = definition_manager.get_by_name(agent["name"])
    if existing is None:
        definition_manager.create(
            name=agent["name"],
            definition_json=json.dumps(definition),
            workflow_type="workflow",
            priority=100,
            enabled=True,
        )
    else:
        definition_manager.update(
            existing.id,
            definition_json=json.dumps(definition),
            workflow_type="workflow",
            priority=100,
            enabled=True,
        )
    manager = WorkflowInstanceManager(db)
    manager.save_instance(
        WorkflowInstance(
            id=f"inst-{session_id}-merge-orchestrator",
            session_id=session_id,
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


def test_merge_orchestrator_allows_already_implemented_close_path() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    survey = _step(agent, "survey")
    plan = _step(agent, "plan")
    report = _step(agent, "report")

    assert 'reason="already_implemented"' in instructions
    assert "Do NOT close_task on the campaign task except" in instructions
    assert "gobby-tasks:close_task" in survey["allowed_mcp_tools"]
    assert "gobby-tasks:close_task" in plan["allowed_mcp_tools"]
    assert "gobby-tasks:close_task" in report["allowed_mcp_tools"]
    assert "gobby-tasks:close_task" not in _step(agent, "execute")["blocked_mcp_tools"]
    assert any(
        transition["to"] == "terminate" and transition["when"] == "vars.report_complete"
        for transition in survey["transitions"]
    )
    assert any(
        transition["to"] == "terminate" and transition["when"] == "vars.report_complete"
        for transition in plan["transitions"]
    )


def test_merge_orchestrator_plan_can_refresh_read_only_survey_state() -> None:
    plan_tools = set(_step(_agent(), "plan")["allowed_mcp_tools"])

    assert "gobby-merge:inspect_merge_state" in plan_tools
    assert "gobby-merge:analyze_merge_landscape" in plan_tools
    assert "gobby-merge:predict_conflicts" in plan_tools
    assert "gobby-merge:probe_branch_protection" in plan_tools
    assert "gobby-tasks-ops:get_artifacts" in plan_tools


def test_merge_orchestrator_loads_build_coordinator_skill_before_agent_queries() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    load_skill = _step(agent, "load_skill")

    assert 'get_skill(name="merge-expert")' in instructions
    assert 'get_skill(name="build-coordinator")' in instructions
    assert "gobby-skills:get_skill" in load_skill["allowed_mcp_tools"]
    assert "vars.skill_loaded and vars.build_coordinator_skill_loaded" in {
        transition["when"] for transition in load_skill["transitions"]
    }
    assert agent["step_variables"]["build_coordinator_skill_loaded"] is False


@pytest.mark.asyncio
async def test_execute_step_allows_contract_tools_and_blocks_lifecycle_hazards(
    db: HubDatabase,
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
async def test_survey_empty_campaign_can_close_already_implemented(
    db: HubDatabase,
) -> None:
    manager = _install_workflow(db, current_step="survey")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _before_mcp_tool("gobby-tasks:close_task"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "allow"

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-tasks:close_task",
            arguments={
                "task_id": "#14063",
                "reason": "already_implemented",
                "changes_summary": "All phase work already landed through child tasks.",
            },
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "terminate"
    assert instance.variables["report_complete"] is True


@pytest.mark.asyncio
async def test_plan_empty_campaign_can_close_already_implemented(
    db: HubDatabase,
) -> None:
    manager = _install_workflow(db, current_step="plan")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _before_mcp_tool("gobby-tasks:close_task"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "allow"

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-tasks:close_task",
            arguments={
                "task_id": "#14063",
                "reason": "already_implemented",
                "changes_summary": "Merge plan is empty because child merges already landed.",
            },
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "terminate"
    assert instance.variables["report_complete"] is True


@pytest.mark.asyncio
async def test_report_can_close_already_implemented_and_terminate(
    db: HubDatabase,
) -> None:
    manager = _install_workflow(db, current_step="report")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _before_mcp_tool("gobby-tasks:close_task"),
        session_id="agent-session",
        variables=variables,
    )
    assert response.decision == "allow"

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-tasks:close_task",
            arguments={
                "task_id": "#14063",
                "reason": "already_implemented",
                "changes_summary": "No merge commit required for this parent phase.",
            },
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "terminate"
    assert instance.variables["report_complete"] is True


@pytest.mark.asyncio
async def test_load_skill_step_waits_for_merge_and_build_coordinator_skills(
    db: HubDatabase,
) -> None:
    manager = _install_workflow(db, current_step="load_skill")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-skills:get_skill",
            arguments={"name": "merge-expert"},
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "load_skill"
    assert instance.variables["skill_loaded"] is True
    assert instance.variables["build_coordinator_skill_loaded"] is False

    await engine.evaluate(
        _after_mcp_tool(
            "gobby-skills:get_skill",
            arguments={"name": "build-coordinator"},
        ),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "survey"
    assert instance.variables["build_coordinator_skill_loaded"] is True


@pytest.mark.asyncio
async def test_merge_orchestrator_survey_plan_execute_report_path(db: HubDatabase) -> None:
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

    for mcp_key in ("gobby-merge:inspect_merge_state", "gobby-tasks-ops:get_artifacts"):
        response = await engine.evaluate(
            _before_mcp_tool(mcp_key),
            session_id="agent-session",
            variables=variables,
        )
        assert response.decision == "allow", mcp_key

    await engine.evaluate(
        _after_set_variable("merge_plan", [{"step_no": 1, "worktree_id": "wt-1"}]),
        session_id="agent-session",
        variables=variables,
    )
    instance = manager.get_instance("agent-session", "merge-orchestrator")
    assert instance is not None
    assert instance.current_step == "execute"

    manager = _install_workflow(db, current_step="plan", session_id="agent-session-json")
    await engine.evaluate(
        _after_set_variable("merge_plan_json", "step_no=1 worktree_id=wt-1"),
        session_id="agent-session-json",
        variables={},
    )
    json_instance = manager.get_instance("agent-session-json", "merge-orchestrator")
    assert json_instance is not None
    assert json_instance.current_step == "execute"

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
    db: HubDatabase,
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
async def test_execute_blocks_no_progress_worker_redispatch(db: HubDatabase) -> None:
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
    db: HubDatabase,
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


def test_merge_orchestrator_preserves_guarded_verify_commands() -> None:
    instructions = " ".join(_agent()["instructions"].split())
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    assert "Preserve any required environment guards" in instructions
    assert "GOBBY_TEST_PROTECT=1 uv run pytest" in instructions
    assert "env GOBBY_TEST_PROTECT=1" in instructions
    assert "Preserve required environment guards" in skill
    assert "GOBBY_TEST_PROTECT=1" in skill


def test_merge_orchestrator_does_not_green_gate_tdd_red_phase_pytest() -> None:
    instructions = " ".join(_agent()["instructions"].split())
    skill = " ".join(SKILL_PATH.read_text(encoding="utf-8").split())

    for text in (instructions, skill):
        assert "TDD red-phase" in text
        assert "expected-failing pytest command" in text
        assert "do not" in text.lower()
        assert "green" in text
        assert "QA red evidence" in text
