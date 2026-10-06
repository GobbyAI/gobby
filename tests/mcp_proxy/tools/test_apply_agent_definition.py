"""Full agent activation, permission and concurrency contracts."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_spawn_tools import register_agent_spawn_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.skills.discovery import get_session_skill_exclusions
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager
from gobby.workflows.definitions import AgentDefinitionBody, AgentSelector, AgentStepWorkflowBody
from gobby.workflows.engine import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def session_id(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> str:
    return register_session(temp_db, monkeypatch)


def register_session(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> str:
    machine = "21000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("gobby.storage.workspace_machine_scope.require_machine_id", lambda: machine)
    LocalMachineManager(temp_db).upsert_seen(machine, TEST_USER_ID)
    return (
        SessionManager(temp_db)
        .register(external_id=str(uuid4()), machine_id=machine, source="codex", project_id=None)
        .id
    )


@pytest.fixture
def definitions(monkeypatch: pytest.MonkeyPatch) -> dict[str, AgentDefinitionBody]:
    bodies = {
        name: make_agent_definition(
            name=name,
            surfaces=["persona"],
            prompts={"persona": f"Instructions for {name}."},
            blocked_tools=["EnterWorktree"],
            blocked_mcp_tools=["gobby-worktrees:create_worktree"],
            workflows=make_agent_workflows(variables={"x_only": 1} if name == "x" else {}),
        )
        for name in ("default", "x", "y", "z")
    }
    monkeypatch.setattr(
        "gobby.workflows.agent_resolver.resolve_agent_with_row",
        lambda name, *args, **kwargs: (bodies[name], SimpleNamespace(step_workflow_id=None))
        if name in bodies
        else None,
    )
    return bodies


def apply_definition() -> Callable[..., Any]:
    return cast(Callable[..., Any], activation_module().apply_agent_definition_impl)


@pytest.mark.asyncio
async def test_activation_writes_full_delta_and_agent_type(
    temp_db: HubDatabase, session_id: str, definitions: dict[str, AgentDefinitionBody]
) -> None:
    manager = SessionVariableManager(temp_db)
    with patch.object(manager.__class__, "merge_variables", wraps=manager.merge_variables) as merge:
        result = await apply_definition()(agent="x", db=temp_db, session_id=session_id)
    assert result["success"] is True
    assert result.get("status") == "applied"
    assert merge.call_count == 1
    variables = manager.get_variables(session_id)
    assert variables["_agent_type"] == "x"
    assert variables["x_only"] == 1
    assert variables["_agent_definition_keys"] == ["x_only"]
    assert variables["_agent_blocked_tools"] == ["EnterWorktree"]
    assert variables["_agent_blocked_mcp_tools"] == ["gobby-worktrees:create_worktree"]
    assert variables["_active_skill_names"] is None
    assert variables["_active_rule_names"] == []
    assert variables["_agent_definition_hash"] == result["definition_hash"]
    assert result["rules_count"] == 0
    assert result["skills_count"] == 0
    assert result["blocked_tools_count"] == 2
    assert result["step_workflow"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown", "surface", "pipeline", "spawned", "task", "collision"])
async def test_refusals_write_nothing(
    temp_db: HubDatabase, session_id: str, definitions: dict[str, AgentDefinitionBody], case: str
) -> None:
    manager = SessionVariableManager(temp_db)
    options: dict[str, Any] = {"agent": "x", "db": temp_db, "session_id": session_id}
    errors = {
        "unknown": "unknown_agent_definition",
        "surface": "persona_surface_missing",
        "pipeline": "pipeline_requires_spawn",
        "spawned": "spawned_session_definition_fixed",
        "task": "task_unresolved",
        "collision": "variable_collision",
    }
    if case == "unknown":
        options["agent"] = "missing"
    elif case == "surface":
        definitions["x"] = make_agent_definition(
            name="x", surfaces=["spawn"], prompts={"agent": "Work."}
        )
    elif case == "pipeline":
        definitions["x"].workflows.pipeline = "planning"
    elif case == "spawned":
        manager.merge_variables(session_id, {"is_spawned_agent": True})
    elif case == "task":
        options["task_id"] = "#123456"
    else:
        options["variables"] = {"x_only": 2}
    before = manager.get_variables(session_id)
    with patch.object(SessionVariableManager, "merge_variables") as merge:
        result = await apply_definition()(**options)
    merge.assert_not_called()
    assert result["success"] is False
    assert result.get("error_code") == errors[case]
    assert manager.get_variables(session_id) == before


@pytest.mark.asyncio
async def test_same_seat_noop_and_role_change_refused(
    temp_db: HubDatabase,
    session_id: str,
    definitions: dict[str, AgentDefinitionBody],
) -> None:
    apply = apply_definition()
    await apply(agent="x", db=temp_db, session_id=session_id)
    manager = SessionVariableManager(temp_db)
    before = manager.get_variables(session_id)
    with (
        patch.object(SessionVariableManager, "merge_variables") as merge,
        patch.object(activation_module(), "_resolve_task_variables") as resolve_task,
    ):
        result = await apply(
            agent="x",
            db=temp_db,
            session_id=session_id,
            variables={"x_only": 9},
            task_id="#unknown",
        )
        assert result.get("status") == "unchanged"
        for name in ("y", "default"):
            result = await apply(agent=name, db=temp_db, session_id=session_id)
            assert result.get("error_code") == "role_change_requires_relaunch"
        merge.assert_not_called()
        resolve_task.assert_not_called()
    assert manager.get_variables(session_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
async def test_activation_preserves_runtime_defaults(
    temp_db: HubDatabase,
    session_id: str,
    definitions: dict[str, AgentDefinitionBody],
    drift: bool,
) -> None:
    from gobby.storage.definitions.variables import SessionVariableDefaultManager

    defaults = SessionVariableDefaultManager(temp_db)
    for name, value in {
        "task_claimed": False,
        "claimed_tasks": {},
        "task_edited_files": {},
        "_agent_context_injected": False,
        "_agent_identity_reinject": False,
    }.items():
        defaults.create(name=name, default_value=value)
    apply = apply_definition()
    if drift:
        await apply(agent="x", db=temp_db, session_id=session_id)
        definitions["x"].description = "Changed definition."
    runtime = {
        "task_claimed": True,
        "claimed_tasks": {"u1": "#1"},
        "task_edited_files": {"u1": ["src/example.py"]},
    }
    manager = SessionVariableManager(temp_db)
    manager.merge_variables(
        session_id,
        {**runtime, "_agent_context_injected": True, "_agent_identity_reinject": False},
    )
    result = await apply(agent="x", db=temp_db, session_id=session_id)
    assert result["status"] == "applied"
    stored = manager.get_variables(session_id)
    assert {key: stored[key] for key in runtime} == runtime
    assert stored["_agent_context_injected"] is False
    assert stored["_agent_identity_reinject"] is True


@pytest.mark.asyncio
async def test_relaunch_preserves_caller_overlay_after_cleanup(
    temp_db: HubDatabase, session_id: str, definitions: dict[str, AgentDefinitionBody]
) -> None:
    apply = apply_definition()
    await apply(agent="x", db=temp_db, session_id=session_id)
    result = await apply(
        agent="y", db=temp_db, session_id=session_id, relaunch=True, variables={"x_only": 7}
    )
    assert result["status"] == "applied"
    assert SessionVariableManager(temp_db).get_variables(session_id)["x_only"] == 7


@pytest.mark.asyncio
async def test_activation_preserves_runtime_update_after_build(
    temp_db: HubDatabase, session_id: str, definitions: dict[str, AgentDefinitionBody]
) -> None:
    from gobby.storage.definitions.variables import SessionVariableDefaultManager

    SessionVariableDefaultManager(temp_db).create(name="task_claimed", default_value=False)
    module = activation_module()
    build = module.build_definition_changes
    manager = SessionVariableManager(temp_db)

    def update_during_build(*args: Any, **kwargs: Any) -> Any:
        result = build(*args, **kwargs)
        manager.merge_variables(session_id, {"task_claimed": True})
        return result

    with patch.object(module, "build_definition_changes", side_effect=update_during_build):
        result = await apply_definition()(agent="x", db=temp_db, session_id=session_id)
    assert result["status"] == "applied"
    assert manager.get_variables(session_id)["task_claimed"] is True


@pytest.mark.asyncio
async def test_definition_variables_overwrite_runtime_on_activation_and_relaunch(
    temp_db: HubDatabase, session_id: str, definitions: dict[str, AgentDefinitionBody]
) -> None:
    definitions["y"].workflows.variables = {"x_only": 2}
    manager = SessionVariableManager(temp_db)
    manager.merge_variables(session_id, {"x_only": 99})
    apply = apply_definition()
    first = await apply(agent="x", db=temp_db, session_id=session_id)
    assert first["status"] == "applied"
    assert manager.get_variables(session_id)["x_only"] == 1
    second = await apply(agent="y", db=temp_db, session_id=session_id, relaunch=True)
    assert second["status"] == "applied"
    assert manager.get_variables(session_id)["x_only"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["y", "default"])
async def test_relaunch_switches_seat_and_clears_previous_keys(
    temp_db: HubDatabase, session_id: str, definitions: dict[str, AgentDefinitionBody], target: str
) -> None:
    apply = apply_definition()
    await apply(agent="x", db=temp_db, session_id=session_id)
    result = await apply(agent=target, db=temp_db, session_id=session_id, relaunch=True)
    assert result.get("status") == "applied"
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["_agent_type"] == target
    assert variables["x_only"] is None
    assert variables["_agent_definition_keys"] == []


def test_concurrent_activations_recheck_permission_under_lock(
    temp_db: HubDatabase,
    session_id: str,
    definitions: dict[str, AgentDefinitionBody],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = activation_module()
    for name in ("y", "z"):
        definitions[name].workflows.variables = {f"{name}_only": name}
    barrier = threading.Barrier(2, timeout=5)
    resolver = module._resolve_task_variables

    def synchronized(*args: Any) -> dict[str, Any]:
        barrier.wait()
        return cast(dict[str, Any], resolver(*args))

    monkeypatch.setattr(module, "_resolve_task_variables", synchronized)
    results: dict[str, dict[str, Any]] = {}
    errors: list[BaseException] = []

    def activate(name: str) -> None:
        try:
            results[name] = asyncio.run(
                module.apply_agent_definition_impl(agent=name, db=temp_db, session_id=session_id)
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=activate, args=(name,)) for name in ("y", "z")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(
        str(result.get("status", result.get("error_code"))) for result in results.values()
    ) == ["activation_superseded", "applied"]
    winner = next(name for name, result in results.items() if result.get("status") == "applied")
    stored = SessionVariableManager(temp_db).get_variables(session_id)
    assert stored["_agent_type"] == winner
    assert stored["_agent_definition_hash"] == module.definition_pin(definitions[winner])
    assert stored["_agent_definition_keys"] == [f"{winner}_only"]
    assert stored[f"{winner}_only"] == winner
    loser = "z" if winner == "y" else "y"
    assert f"{loser}_only" not in stored


@pytest.mark.asyncio
async def test_activation_over_configured_base_clears_base_keys(
    temp_db: HubDatabase,
    session_id: str,
    definitions: dict[str, AgentDefinitionBody],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionVariableManager(temp_db)
    definitions["configured-base"] = make_agent_definition(
        name="configured-base",
        surfaces=["persona"],
        prompts={"persona": "Base instructions."},
        workflows=make_agent_workflows(variables={"base_only": 1}),
    )
    monkeypatch.setattr(
        "gobby.storage.config_repository.ConfigRepository.read",
        lambda *args, **kwargs: SimpleNamespace(values={"default_agent": "configured-base"}),
    )
    base = await apply_definition()(agent="configured-base", db=temp_db, session_id=session_id)
    assert base["status"] == "applied"
    assert manager.get_variables(session_id)["_agent_definition_keys"] == ["base_only"]
    assert manager.get_variables(session_id)["base_only"] == 1
    result = await apply_definition()(agent="y", db=temp_db, session_id=session_id)
    assert result.get("status") == "applied"
    assert manager.get_variables(session_id)["base_only"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["agent_depth", "agent_run_id"])
async def test_spawned_row_refused_without_variable_flag(
    temp_db: HubDatabase,
    session_id: str,
    definitions: dict[str, AgentDefinitionBody],
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    row = SimpleNamespace(agent_depth=0, agent_run_id=None)
    setattr(row, marker, 1 if marker == "agent_depth" else str(uuid4()))
    monkeypatch.setattr(SessionManager, "get", lambda *args: row)
    result = await apply_definition()(agent="y", db=temp_db, session_id=session_id)
    assert result.get("error_code") == "spawned_session_definition_fixed"
    assert SessionVariableManager(temp_db).get_variables(session_id) == {}


def activation_module() -> Any:
    return importlib.import_module("gobby.mcp_proxy.tools.apply_agent_definition")


@pytest.mark.asyncio
async def test_activation_clears_inherited_skill_restriction(
    temp_db: HubDatabase, session_id: str, definitions: dict[str, AgentDefinitionBody]
) -> None:
    definitions["default"].workflows.skill_selectors = AgentSelector(include=[])
    definitions["default"].workflows.skill_format = "summary"
    await apply_definition()(agent="default", db=temp_db, session_id=session_id)
    manager = SessionVariableManager(temp_db)
    assert manager.get_variables(session_id)["_active_skill_names"] == []
    assert manager.get_variables(session_id)["_skill_format"] == "summary"
    await apply_definition()(agent="x", db=temp_db, session_id=session_id)
    after = manager.get_variables(session_id)
    assert after["_active_skill_names"] is None
    assert after["_skill_format"] is None


def test_full_builder_resets_absent_restrictions(temp_db: HubDatabase) -> None:
    module = activation_module()
    builder = module.build_definition_changes
    body = make_agent_definition(
        name="seat", surfaces=["persona"], prompts={"persona": "Seat instructions."}
    )
    changes, _, skills = builder(
        body,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
        temp_db,
        enabled_rules=[],
        all_skills=[],
        enabled_variables=[],
    )
    assert skills is None
    assert changes.get("_active_skill_names", "missing") is None
    assert changes.get("_skill_format", "missing") is None
    assert changes.get("_agent_blocked_tools") == []
    assert changes.get("_agent_blocked_mcp_tools") == []
    assert changes.get("_agent_definition_hash")
    assert changes.get("_agent_definition_keys") == []


def assert_worktree_tools_blocked(db: HubDatabase, sid: str) -> None:
    variables = SessionVariableManager(db).get_variables(sid)
    engine = RuleEngine(MagicMock())
    for data in (
        {"tool_name": "EnterWorktree", "tool_input": {}},
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-worktrees",
                "tool_name": "create_worktree",
                "arguments": {},
            },
        },
    ):
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=sid,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data=data,
        )
        response = engine._check_agent_tool_enforcement(event, sid, variables)
        assert response is not None
        assert response.decision == "block"
        assert "agent-enforcement:x" in (response.reason or "")


@pytest.mark.asyncio
async def test_blocked_tools_and_skill_exclusions_follow_seat(
    temp_db: HubDatabase,
    session_id: str,
    definitions: dict[str, AgentDefinitionBody],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definitions["x"].workflows.skill_selectors = AgentSelector(exclude=["name:hidden"])
    LocalSkillManager(temp_db).create_skill(name="hidden", description="Hidden", content="# Hidden")
    LocalSkillManager(temp_db).create_skill(
        name="visible", description="Visible", content="# Visible"
    )
    monkeypatch.setattr(
        "gobby.skills.discovery.resolve_agent", lambda name, *args, **kwargs: definitions[name]
    )
    await apply_definition()(agent="x", db=temp_db, session_id=session_id)
    assert_worktree_tools_blocked(temp_db, session_id)
    assert get_session_skill_exclusions(temp_db, session_id, None) == {"hidden"}
    await apply_definition()(agent="y", db=temp_db, session_id=session_id, relaunch=True)
    assert get_session_skill_exclusions(temp_db, session_id, None) == set()


@pytest.mark.asyncio
async def test_activated_seat_refuses_worktree_tools(
    temp_db: HubDatabase,
    session_id: str,
) -> None:
    from gobby.storage.definitions import AgentDefinitionManager
    from tests.hooks.test_session_start_reactivation import compact

    body = make_agent_definition(
        name="x",
        surfaces=["persona"],
        prompts={"persona": "Work."},
        blocked_tools=["EnterWorktree"],
        blocked_mcp_tools=["gobby-worktrees:create_worktree"],
    )
    AgentDefinitionManager(temp_db).create(
        name="x", definition_json=body.model_dump_json(), source="custom"
    )
    await apply_definition()(agent="x", db=temp_db, session_id=session_id)
    assert_worktree_tools_blocked(temp_db, session_id)
    compact(temp_db, session_id)
    assert_worktree_tools_blocked(temp_db, session_id)


def test_registry_exposes_definition_and_persona() -> None:
    registry = InternalToolRegistry("gobby-agents")
    context = cast(AgentsRegistryContext, MagicMock())
    with patch(
        "gobby.mcp_proxy.tools.spawn_agent.create_spawn_agent_registry",
        return_value=InternalToolRegistry("spawn"),
    ):
        register_agent_spawn_tools(registry, context)
    persona = "apply_persona"
    names = {tool["name"] for tool in registry.list_tools()}
    assert "apply_agent_definition" in names
    assert persona in names
    assert importlib.util.find_spec(f"gobby.mcp_proxy.tools.{persona}") is not None


class TestBuildDefinitionChanges:
    """Tests for the shared build_definition_changes function."""

    def test_sets_agent_type_and_rules(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="developer",
        )
        changes, active_rules, active_skills = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_agent_type"] == "developer"
        assert "_active_rule_names" in changes
        assert changes["is_spawned_agent"] is False

    def test_spawned_flag(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="worker",
        )
        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
            is_spawned=True,
        )

        assert changes["is_spawned_agent"] is True

    def test_merges_agent_variables(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="custom",
            workflows=make_agent_workflows(
                variables={"my_var": "hello", "another": 42},
            ),
        )
        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["my_var"] == "hello"
        assert changes["another"] == 42

    def test_skips_reserved_variables(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="custom",
            workflows=make_agent_workflows(
                variables={"_reserved": "bad", "good_var": "ok"},
            ),
        )
        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert "_reserved" not in changes
        assert changes["good_var"] == "ok"

    def test_blocked_tools(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="restricted",
            blocked_tools=["Write", "Bash"],
            blocked_mcp_tools=["gobby-tasks:delete_task"],
        )
        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_agent_blocked_tools"] == ["Write", "Bash"]
        assert changes["_agent_blocked_mcp_tools"] == ["gobby-tasks:delete_task"]

    def test_skill_format_override(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="compact",
            workflows=make_agent_workflows(skill_format="compact"),
        )
        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_skill_format"] == "compact"

    def test_step_completion_seeded_for_caller_persona(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes
        from gobby.workflows.definitions import WorkflowStep

        # Create a project + session so FK constraints are satisfied
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            ("11111111-1111-4111-8111-111111110001", "test-project"),
        )
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4002"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_id,
                "ext-1",
                "11111111-1111-4111-8111-111111110001",
                "21000000-0000-4000-8000-000000000001",
                "test",
                "active",
            ),
        )

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="stepper",
            step_workflow=AgentStepWorkflowBody(
                steps=[
                    WorkflowStep(name="plan", instructions="Plan the work"),
                    WorkflowStep(name="execute", instructions="Do the work"),
                ],
            ),
        )
        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
        )

        assert "_step_workflow_name" not in changes
        assert changes["step_workflow_complete"] is False

        from gobby.workflows.step_instances import AgentStepInstanceManager

        instance = AgentStepInstanceManager(db).get_for_session(session_id)
        assert instance is None

    @pytest.mark.parametrize(
        "task_variables",
        [{}, {"assigned_task_id": None, "active_task_id": None}],
        ids=["missing", "json-null"],
    )
    def test_step_completion_seeded_for_taskless_spawn(
        self,
        db: HubDatabase,
        task_variables: dict[str, object],
    ) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes
        from gobby.workflows.definitions import WorkflowStep
        from gobby.workflows.state_manager import SessionVariableManager
        from gobby.workflows.step_instances import AgentStepInstanceManager

        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            ("11111111-1111-4111-8111-111111110004", "taskless-project"),
        )
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4005"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_id,
                "ext-taskless",
                "11111111-1111-4111-8111-111111110004",
                "21000000-0000-4000-8000-000000000001",
                "test",
                "active",
            ),
        )
        SessionVariableManager(db).merge_variables(session_id, task_variables)
        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="stepper",
            step_workflow=AgentStepWorkflowBody(
                steps=[WorkflowStep(name="plan", instructions="Plan the work")],
            ),
        )

        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
            is_spawned=True,
        )

        assert "_step_workflow_name" not in changes
        assert changes["step_workflow_complete"] is False
        instance = AgentStepInstanceManager(db).get_for_session(session_id)
        assert instance is None

    def test_spawned_session_preserves_existing_step_workflow(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes
        from gobby.workflows.definitions import WorkflowStep
        from gobby.workflows.state_manager import SessionVariableManager
        from gobby.workflows.step_instances import AgentStepInstanceManager
        from tests.workflows.step_instance_fixtures import make_step_instance

        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            ("11111111-1111-4111-8111-111111110003", "test-project-preserve"),
        )
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4004"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_id,
                "ext-preserve",
                "11111111-1111-4111-8111-111111110003",
                "21000000-0000-4000-8000-000000000001",
                "codex",
                "active",
            ),
        )
        SessionVariableManager(db).merge_variables(session_id, {"assigned_task_id": "#20144"})

        instance_mgr = AgentStepInstanceManager(db)
        instance_mgr.save(
            make_step_instance(
                session_id,
                agent_name="stepper",
                current_step="execute",
                variables={"task_claimed": True},
            )
        )

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="stepper",
            step_workflow=AgentStepWorkflowBody(
                variables={"task_claimed": False, "loaded_skills": []},
                steps=[
                    WorkflowStep(name="claim", instructions="Claim the task"),
                    WorkflowStep(name="execute", instructions="Do the work"),
                ],
            ),
        )

        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
            is_spawned=True,
        )

        instance = instance_mgr.get_for_session(session_id)
        assert instance is not None
        assert instance.current_step == "execute"
        assert instance.variables.get("task_claimed") is True

    def test_uses_preloaded_rules_and_skills(self, db: HubDatabase) -> None:
        """When enabled_rules and all_skills are passed, DB is not queried."""
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="test",
        )
        changes, active_rules, active_skills = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
            enabled_rules=[],
            all_skills=[],
            enabled_variables=[],
        )

        assert changes["_agent_type"] == "test"
        assert active_rules == set()

    def test_db_variable_definitions(self, db: HubDatabase) -> None:
        """Variable definitions from the DB get applied."""
        from gobby.mcp_proxy.tools.apply_agent_definition import build_definition_changes
        from gobby.storage.definitions import SessionVariableDefaultManager

        SessionVariableDefaultManager(db).create(name="my_db_var", default_value="from_db")

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="test",
        )
        changes, _, _ = build_definition_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes.get("my_db_var") == "from_db"
