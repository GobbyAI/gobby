"""Running seats execute the installed definition while retaining activation provenance."""

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.apply_agent_definition import (
    build_definition_changes,
    commit_definition_changes,
    definition_pin,
)
from gobby.mcp_proxy.tools.workflows._import import reload_cache
from gobby.storage.definitions._shared import encode_json_value
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_context import get_active_step_workflow_context
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

pytestmark = pytest.mark.integration


def _definition(revision: str, tool: str) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "refresh-seat",
            "prompts": {"agent": f"Follow revision {revision}."},
            "workflows": {"rule_selectors": {"include": []}},
            "step_workflow": {
                "variables": {"handed_off": False},
                "exit_condition": "vars.finished",
                "steps": [
                    {
                        "name": "submit",
                        "status_message": f"Submit under {revision}.",
                        "allowed_tools": ["mcp__gobby__call_tool"],
                        "allowed_mcp_tools": [f"gobby-tasks:{tool}"],
                        "on_mcp_success": [
                            {
                                "server": "gobby-tasks",
                                "tool": tool,
                                "action": "set_variable",
                                "variable": "handed_off",
                                "value": True,
                            }
                        ],
                        "transitions": [{"to": "review", "when": "vars.handed_off"}],
                    },
                    {"name": "review", "status_message": f"Review under {revision}."},
                ],
            },
        }
    )


@pytest.fixture
def seat_ids(hub_db: HubDatabase) -> tuple[str, str]:
    session_id, project_id = str(uuid4()), str(uuid4())
    hub_db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (project_id, "refresh-project"),
    )
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (session_id, session_id, "21000000-0000-4000-8000-000000000001", "claude", project_id),
    )
    return session_id, project_id


@pytest.mark.parametrize("recreate_engine", [False, True], ids=["reload-cache", "recreated-engine"])
@pytest.mark.asyncio
async def test_reloaded_reviewer_ends_only_on_terminal_event(
    hub_db: HubDatabase, tmp_path: Path, recreate_engine: bool, seat_ids: tuple[str, str]
) -> None:
    session_id, _ = seat_ids
    current = AgentDefinitionBody.model_validate(
        yaml.safe_load((get_bundled_agents_path() / "code-reviewer.yaml").read_text())
    )
    current.workflows.rule_selectors.include = []
    original = current.model_copy(deep=True)
    assert original.step_workflow is not None
    verdict = next(step for step in original.step_workflow.steps if step.name == "verdict")
    for handler in verdict.on_mcp_success:
        handler["when"] = (
            "('VERDICT=BOUNCE' in str(tool_input.get('content')) or "
            "'EVENT=LANDED' in str(tool_input.get('content'))) "
            "and str(vars.candidate_task) in str(tool_input.get('content'))"
        )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    template = agents_dir / "code-reviewer.yaml"
    template.write_text(yaml.safe_dump(original.model_dump(mode="json")), encoding="utf-8")
    instance_mgr = AgentStepInstanceManager(hub_db)
    variables = {"candidate_task": "#42", "candidate_received": True, "verdict_ready": True}
    with (
        patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir),
        patch(
            "gobby.sync_registry.SYNC_TARGETS",
            [("agents", "gobby.agents.sync", "sync_bundled_agents")],
        ),
        patch(
            "gobby.mcp_proxy.tools.workflows._import.sync_imported_workflows",
            return_value={"synced": 0, "errors": []},
        ),
    ):
        assert sync_bundled_agents(hub_db)["errors"] == []
        row = AgentDefinitionManager(hub_db).get_by_name(original.name)
        assert row is not None
        instance = build_step_instance(
            original,
            session_id=session_id,
            step_workflow_id=row.step_workflow_id,
            current_step="verdict",
            variables=variables,
        )
        instance.step_action_count = 3
        instance.total_action_count = 7
        instance_mgr.save(instance)
        before = instance.model_dump()
        engine = RuleEngine(hub_db)
        installed = resolve_agent(original.name, hub_db)
        assert installed is not None and installed.step_workflow == original.step_workflow
        template.write_text(yaml.safe_dump(current.model_dump(mode="json")), encoding="utf-8")
        synced = reload_cache(MagicMock(), db=hub_db)
        assert synced["success"] is True, synced
        installed = resolve_agent(current.name, hub_db)
        assert installed is not None and installed.step_workflow == current.step_workflow

    if recreate_engine:
        engine = RuleEngine(hub_db)
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-agents",
                "tool_name": "send_message",
                "arguments": {
                    "content": "EVENT=CANDIDATE_VERDICT TASK=#42 SHA=abc VERDICT=LAND "
                    "NOTE=No VERDICT=BOUNCE needed; EVENT=LANDED follows"
                },
            },
            "tool_output": {"success": True},
        },
    )
    await engine.evaluate(event, session_id=session_id, variables={})
    retained = instance_mgr.get_for_session(session_id)
    assert retained is not None
    assert retained.current_step == "verdict"
    assert retained.variables == before["variables"]
    assert retained.snapshot == instance.snapshot
    assert retained.id == instance.id
    assert retained.step_entered_at == instance.step_entered_at
    assert retained.step_action_count == instance.step_action_count
    assert retained.total_action_count == instance.total_action_count

    event.data["tool_input"]["arguments"]["content"] = (
        "EVENT=LANDED TASK=#42 SHA=abc RECEIPT=receipt RETEST=PASS"
    )
    await engine.evaluate(event, session_id=session_id, variables={})
    completed = instance_mgr.get_for_session(session_id)
    assert completed is not None and completed.current_step == "await"
    assert completed.variables["candidate_received"] is False
    assert completed.variables["verdict_ready"] is False
    assert completed.variables["candidate_task"] is None
    assert completed.snapshot == instance.snapshot and completed.id == instance.id


@pytest.mark.parametrize("recreate_engine", [False, True], ids=["reload-cache", "recreated-engine"])
@pytest.mark.asyncio
async def test_running_seat_uses_synced_definition(
    hub_db: HubDatabase, tmp_path: Path, recreate_engine: bool, seat_ids: tuple[str, str]
) -> None:
    session_id, project_id = seat_ids
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    template = agents_dir / "refresh-seat.yaml"
    original = _definition("R1", "get_task")
    template.write_text(yaml.safe_dump(original.model_dump(mode="json")), encoding="utf-8")
    with (
        patch("gobby.agents.sync.get_bundled_agents_path", return_value=agents_dir),
        patch(
            "gobby.sync_registry.SYNC_TARGETS",
            [("agents", "gobby.agents.sync", "sync_bundled_agents")],
        ),
        patch(
            "gobby.mcp_proxy.tools.workflows._import.sync_imported_workflows",
            return_value={"synced": 0, "errors": []},
        ),
    ):
        assert sync_bundled_agents(hub_db)["errors"] == []
        row = AgentDefinitionManager(hub_db).get_by_name(original.name)
        assert row is not None
        instance = build_step_instance(
            original,
            session_id=session_id,
            step_workflow_id=row.step_workflow_id,
            current_step="submit",
            variables={"assigned_task_id": "retained-task"},
        )
        instance.step_action_count = 3
        instance.total_action_count = 7
        instance_mgr = AgentStepInstanceManager(hub_db)
        instance_mgr.save(instance)
        before = instance.model_dump()
        variable_mgr = SessionVariableManager(hub_db)
        initial_changes, _, _ = build_definition_changes(original, session_id, hub_db)
        variable_mgr.merge_variables(session_id, {**initial_changes, "finished": False})
        engine = RuleEngine(hub_db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "escalate_task"},
            },
        )
        denied = await engine.evaluate(event, session_id=session_id, variables={})
        assert denied.decision == "block"
        assert denied.reason is not None and "gobby-tasks:get_task" in denied.reason

        updated = _definition("R2", "escalate_task")
        template.write_text(yaml.safe_dump(updated.model_dump(mode="json")), encoding="utf-8")
        synced = reload_cache(MagicMock(), db=hub_db)
        assert synced["success"] is True
        assert synced["agents_synced"] == 1

    current = resolve_agent(original.name, hub_db, project_id=project_id)
    assert current is not None
    if recreate_engine:
        engine = RuleEngine(hub_db)
    after_sync = await engine.evaluate(
        event, session_id=session_id, variables=variable_mgr.get_variables(session_id)
    )
    assert after_sync.decision == "allow"
    assert variable_mgr.get_variables(session_id)["_agent_definition_hash"] == definition_pin(
        original
    )
    changes, _, _ = build_definition_changes(current, session_id, hub_db)
    activated = commit_definition_changes(
        hub_db,
        session_id,
        original.name,
        changes,
        expected_agent_type=original.name,
        relaunch=False,
        same_pin_noop=True,
    )
    assert activated["status"] == "applied"
    variables = variable_mgr.get_variables(session_id)
    assert variables["_agent_definition_hash"] == definition_pin(current)
    assert definition_pin(current)[:12] in variables["_agent_definition_drift"]
    assert "the current definition now applies" in variables["_agent_definition_drift"]
    allowed = await engine.evaluate(event, session_id=session_id, variables=variables)
    assert allowed.decision == "allow"
    old_event = deepcopy(event)
    old_event.data["tool_input"]["tool_name"] = "get_task"
    revoked = await engine.evaluate(old_event, session_id=session_id, variables=variables)
    assert revoked.decision == "block"
    assert revoked.reason is not None and "gobby-tasks:escalate_task" in revoked.reason
    context = get_active_step_workflow_context(hub_db, session_id)
    assert context is not None and context.status_message == "Submit under R2."
    retained = instance_mgr.get_for_session(session_id)
    assert retained is not None and retained.model_dump() == before

    completed_event = deepcopy(event)
    completed_event.event_type = HookEventType.AFTER_TOOL
    completed_event.data["tool_output"] = {"success": True}
    await engine.evaluate(completed_event, session_id=session_id, variables=variables)
    context = get_active_step_workflow_context(hub_db, session_id)
    assert context is not None and context.current_step == "review"
    assert context.status_message == "Review under R2."
    retained = instance_mgr.get_for_session(session_id)
    assert retained is not None
    assert retained.variables["handed_off"] is True
    assert retained.variables["assigned_task_id"] == "retained-task"
    assert retained.snapshot == instance.snapshot
    assert retained.id == instance.id


@pytest.mark.parametrize("remove_step", [False, True], ids=["project-policy", "removed-step"])
@pytest.mark.asyncio
async def test_project_definition_controls_running_seat(
    hub_db: HubDatabase, seat_ids: tuple[str, str], remove_step: bool
) -> None:
    session_id, project_id = seat_ids
    original = _definition("global", "get_task")
    assert original.step_workflow is not None
    manager = AgentDefinitionManager(hub_db)
    row = manager.upsert_with_steps(
        original.name,
        original.model_dump(),
        original.step_workflow.model_dump(),
    )
    instance = build_step_instance(
        original,
        session_id=session_id,
        step_workflow_id=row.step_workflow_id,
        current_step="submit",
    )
    instance_mgr = AgentStepInstanceManager(hub_db)
    instance_mgr.save(instance)
    project_definition = _definition("project", "escalate_task")
    assert project_definition.step_workflow is not None
    if remove_step:
        project_definition.step_workflow.steps.pop(0)
    manager.upsert_with_steps(
        original.name,
        project_definition.model_dump(),
        project_definition.step_workflow.model_dump(),
        project_id=project_id,
    )
    engine = RuleEngine(hub_db)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "escalate_task"},
        },
    )
    result = await engine.evaluate(event, session_id=session_id, variables={})
    assert result.decision == ("block" if remove_step else "allow")
    event.data["tool_input"]["tool_name"] = "get_task"
    revoked = await engine.evaluate(event, session_id=session_id, variables={})
    assert revoked.decision == "block"
    event.data["tool_input"] = {"server_name": "gobby-sessions", "tool_name": "get_handoff"}
    recovery = await engine.evaluate(event, session_id=session_id, variables={})
    assert recovery.decision == "allow"
    if remove_step:
        context = get_active_step_workflow_context(hub_db, session_id)
        assert context is not None and context.current_step == "submit"
        assert context.status_message is not None
        assert "removed step 'submit'" in context.status_message
        assert "handoff and memory access" in context.status_message
        assert "end_agent_run with blockers" in context.status_message
        enforced_step, _ = engine._get_step_for_session(session_id)
        assert enforced_step is not None
        assert enforced_step.status_message == context.status_message
        assert enforced_step.description == context.description
        assert enforced_step.allowed_tools == context.allowed_tools
        assert enforced_step.allowed_mcp_tools == []
        assert "parent or operator" in context.status_message
    retained = instance_mgr.get_for_session(session_id)
    assert retained is not None and retained.snapshot == instance.snapshot


@pytest.mark.parametrize("invalid_row", [True, False], ids=["unresolvable-row", "removed-workflow"])
@pytest.mark.asyncio
async def test_unavailable_current_workflow_retains_snapshot_permissions(
    hub_db: HubDatabase, seat_ids: tuple[str, str], invalid_row: bool
) -> None:
    session_id, _ = seat_ids
    original = _definition("R1", "get_task")
    assert original.step_workflow is not None
    manager = AgentDefinitionManager(hub_db)
    row = manager.upsert_with_steps(
        original.name, original.model_dump(), original.step_workflow.model_dump()
    )
    instance = build_step_instance(
        original,
        session_id=session_id,
        step_workflow_id=row.step_workflow_id,
        current_step="submit",
    )
    instance_mgr = AgentStepInstanceManager(hub_db)
    instance_mgr.save(instance)
    updated = _definition("R2", "escalate_task")
    assert updated.step_workflow is not None
    body = updated.model_dump()
    manager.upsert_with_steps(
        original.name, body, updated.step_workflow.model_dump() if invalid_row else None
    )
    if invalid_row:
        # Reproduce a pre-existing row; the public write boundary rejects this field.
        body["terminal_backend"] = "tmux"
        with hub_db.transaction() as conn:
            conn.execute(
                "UPDATE agent_definitions SET definition_json = %s WHERE id = %s",
                (encode_json_value(body), row.id),
            )
    engine = RuleEngine(hub_db)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "escalate_task"},
        },
    )
    denied = await engine.evaluate(event, session_id=session_id, variables={})
    assert denied.decision == "block"
    assert denied.reason is not None and "gobby-tasks:get_task" in denied.reason
    event.data["tool_input"]["tool_name"] = "get_task"
    allowed = await engine.evaluate(event, session_id=session_id, variables={})
    assert allowed.decision == "allow"
    event.data["tool_input"] = {"server_name": "gobby-sessions", "tool_name": "get_handoff"}
    recovery = await engine.evaluate(event, session_id=session_id, variables={})
    assert recovery.decision == "allow"
    retained = instance_mgr.get_for_session(session_id)
    assert retained is not None and retained.snapshot == instance.snapshot
