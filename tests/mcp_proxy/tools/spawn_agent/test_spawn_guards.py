from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.mcp_proxy.tools.spawn_agent import _factory, _spawn_guards
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import resolve_spawn_task_context
from gobby.sessions.clear_continuation import stage_clear_attempt, take_clear_handoff_marker
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
    WorkflowStep,
)
from tests.fixtures.agent_definitions import make_agent_definition


@dataclass
class _SpawnCaller:
    sessions: SessionManager
    root: Session
    child: Session


def _clear_spawn_session(sessions: SessionManager, predecessor: Session) -> Session:
    successor = sessions.register(
        f"clear-{predecessor.external_id}",
        predecessor.machine_id,
        predecessor.source,
        project_id=predecessor.project_id,
        agent_depth=predecessor.agent_depth,
    )
    if predecessor.agent_run_id is not None:
        sessions.update_terminal_pickup_metadata(
            successor.id, agent_run_id=predecessor.agent_run_id
        )
    attempt = uuid4().hex
    stage_clear_attempt(
        sessions.db,
        predecessor.id,
        attempt_id=attempt,
        handoff=build_handoff_payload(current_state="Continue.", next_steps=["Continue."]),
        terminal_context=None,
        chat_context=None,
    )
    assert take_clear_handoff_marker(
        sessions.db, predecessor.id, attempt_id=attempt, successor_id=successor.id
    )
    updated = sessions.get(successor.id)
    assert updated is not None and updated.parent_session_id == predecessor.id
    return updated


@pytest.mark.asyncio
@pytest.mark.parametrize("cleared", ["root", "coordinator", "caller"])
@pytest.mark.parametrize("allowed", [False, True], ids=["forbidden-target", "allowed-target"])
async def test_spawn_scope_survives_clear(
    spawn_caller: _SpawnCaller,
    cleared: str,
    allowed: bool,
) -> None:
    sessions = spawn_caller.sessions
    if cleared == "root":
        caller = _clear_spawn_session(sessions, spawn_caller.root)
    elif cleared == "coordinator":
        _clear_spawn_session(sessions, spawn_caller.root)
        caller = spawn_caller.child
    else:
        caller = _clear_spawn_session(sessions, spawn_caller.child)
    target = "scope-target" if allowed else "forbidden"
    with session_context_for_test(caller.id):
        if allowed or cleared == "root":
            assert (
                await _spawn_guards.enforce_spawn_caller(sessions, target, caller.project_id)
                == caller.id
            )
        else:
            with pytest.raises(ValueError, match="spawnable_agents"):
                await _spawn_guards.enforce_spawn_caller(sessions, target, caller.project_id)


def _scope_definition(
    db: HubDatabase,
    project_id: str,
    name: str,
    spawnable: list[str],
    fallback: str | None = None,
) -> None:
    AgentDefinitionManager(db).create(
        name,
        {
            "name": name,
            "provider": "claude",
            "prompts": {"agent": "Work."},
            "workflows": {"rule_selectors": {"include": []}},
            "spawnable_agents": spawnable,
            "fallback_agent": fallback,
        },
        project_id=project_id,
    )


@pytest.fixture
def spawn_caller(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _SpawnCaller:
    sessions = SessionManager(temp_db)
    project_id = str(sample_project["id"])
    root = sessions.register("scope-root", None, "test", project_id=project_id)
    child = sessions.register(
        "scope-child",
        None,
        "test",
        project_id=project_id,
        parent_session_id=root.id,
        agent_depth=1,
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=root.id,
        provider="claude",
        prompt="work",
        agent_name="scope-caller",
        child_session_id=child.id,
    )
    updated = sessions.update_terminal_pickup_metadata(child.id, agent_run_id=run.id)
    assert updated is not None
    _scope_definition(temp_db, project_id, "scope-caller", ["scope-target", "allowed-backup"])
    _scope_definition(temp_db, project_id, "scope-target", [], "allowed-backup")
    _scope_definition(temp_db, project_id, "allowed-backup", [])
    return _SpawnCaller(sessions, root, updated)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_session", [False, True])
async def test_rejected_spawn_principal_never_consults_session(
    monkeypatch: pytest.MonkeyPatch,
    spawn_caller: _SpawnCaller,
    with_session: bool,
) -> None:
    monkeypatch.setattr(
        _spawn_guards, "get_request_principal", AsyncMock(return_value=False), raising=False
    )
    context = MagicMock(side_effect=AssertionError("session consulted after rejected credentials"))
    monkeypatch.setattr(_spawn_guards, "get_current_session_id", context)
    with session_context_for_test(spawn_caller.root.id if with_session else ""):
        with pytest.raises(ValueError, match="spawnable_agents") as refused:
            await _spawn_guards.enforce_spawn_caller(spawn_caller.sessions, "scope-target", None)
    assert "rejected request credentials" in str(refused.value)
    context.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", ["operator", "internal", "managed", "managed-no-context"])
async def test_spawn_principal_resolution(
    monkeypatch: pytest.MonkeyPatch,
    spawn_caller: _SpawnCaller,
    principal: str,
) -> None:
    child = spawn_caller.child
    claims = AgentApiTokenClaims(
        child.id, child.project_id, str(child.machine_id), 1, 2, agent_run_id=child.agent_run_id
    )
    managed = principal.startswith("managed")
    resolver = AsyncMock(return_value=claims if managed else None)
    if principal == "internal":
        resolver.side_effect = LookupError("no request principal")
    monkeypatch.setattr(_spawn_guards, "get_request_principal", resolver, raising=False)
    if principal == "managed-no-context":
        monkeypatch.setattr(_spawn_guards, "get_current_session_id", lambda: None)
    with session_context_for_test(child.id if principal == "managed" else ""):
        caller_id = await _spawn_guards.enforce_spawn_caller(
            spawn_caller.sessions, "scope-target", child.project_id
        )
    assert caller_id == (child.id if managed else None)
    resolver.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("root", [False, True])
async def test_seeded_spawn_caller_scope(
    monkeypatch: pytest.MonkeyPatch,
    spawn_caller: _SpawnCaller,
    root: bool,
) -> None:
    monkeypatch.setattr(
        _spawn_guards, "get_request_principal", AsyncMock(return_value=None), raising=False
    )
    caller = spawn_caller.root if root else spawn_caller.child
    with session_context_for_test(caller.id):
        if root:
            await _spawn_guards.enforce_spawn_caller(spawn_caller.sessions, "forbidden", None)
        else:
            with pytest.raises(ValueError, match="spawnable_agents"):
                await _spawn_guards.enforce_spawn_caller(
                    spawn_caller.sessions, "forbidden", caller.project_id
                )


@pytest.mark.asyncio
async def test_bundled_developer_can_admit_only_its_close_reviewer(
    monkeypatch: pytest.MonkeyPatch, spawn_caller: _SpawnCaller
) -> None:
    monkeypatch.setattr(
        _spawn_guards, "get_request_principal", AsyncMock(return_value=None), raising=False
    )
    definition = yaml.safe_load((get_bundled_agents_path() / "developer.yaml").read_text())
    manager = AgentDefinitionManager(spawn_caller.sessions.db)
    row = manager.get_by_name("scope-caller", project_id=spawn_caller.child.project_id)
    assert row is not None
    manager.update(row.id, definition_json={**definition, "name": "scope-caller"})
    _scope_definition(
        spawn_caller.sessions.db, spawn_caller.child.project_id, TASK_CLOSE_REVIEWER_AGENT, []
    )
    with session_context_for_test(spawn_caller.child.id):
        admitted = await _spawn_guards.enforce_spawn_caller(
            spawn_caller.sessions, TASK_CLOSE_REVIEWER_AGENT, spawn_caller.child.project_id
        )
        assert admitted == spawn_caller.child.id
        with pytest.raises(ValueError, match="spawnable_agents"):
            await _spawn_guards.enforce_spawn_caller(
                spawn_caller.sessions, "scope-target", spawn_caller.child.project_id
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        "unresolved",
        "runless",
        "forged-run",
        "token-project",
        "token-run",
        "token-session",
        "target-unresolved",
    ],
)
async def test_spawn_caller_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    spawn_caller: _SpawnCaller,
    identity: str,
) -> None:
    caller = spawn_caller.child
    caller_id = caller.id
    principal: AgentApiTokenClaims | None = None
    target_project_id: str | None = caller.project_id
    if identity == "unresolved":
        caller_id = "missing-caller"
    elif identity == "runless":
        caller_id = spawn_caller.sessions.register(
            "runless",
            None,
            "test",
            project_id=caller.project_id,
            parent_session_id=spawn_caller.root.id,
            agent_depth=1,
        ).id
    elif identity == "forged-run":
        other = spawn_caller.sessions.register(
            "forged",
            None,
            "test",
            project_id=caller.project_id,
            parent_session_id=spawn_caller.root.id,
            agent_depth=1,
        )
        spawn_caller.sessions.update_terminal_pickup_metadata(
            other.id, agent_run_id=caller.agent_run_id
        )
        caller_id = other.id
    elif identity == "target-unresolved":
        target_project_id = None
    else:
        principal = AgentApiTokenClaims(
            caller.id,
            spawn_caller.root.id if identity == "token-project" else caller.project_id,
            str(caller.machine_id),
            1,
            2,
            agent_run_id=spawn_caller.root.id if identity == "token-run" else caller.agent_run_id,
        )
        if identity == "token-session":
            caller_id = spawn_caller.root.id
    monkeypatch.setattr(
        _spawn_guards, "get_request_principal", AsyncMock(return_value=principal), raising=False
    )
    with session_context_for_test(caller_id):
        with pytest.raises(ValueError, match="spawnable_agents"):
            await _spawn_guards.enforce_spawn_caller(
                spawn_caller.sessions, "scope-target", target_project_id
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["explicit-path", "parent-project"])
@pytest.mark.parametrize("allowed", [False, True], ids=["forbidden-fallback", "allowed-fallback"])
async def test_spawn_guard_checks_the_actual_target_project(
    monkeypatch: pytest.MonkeyPatch,
    spawn_caller: _SpawnCaller,
    temp_db: HubDatabase,
    source: str,
    allowed: bool,
) -> None:
    project = LocalProjectManager(temp_db).create("scope-target-project")
    parent = spawn_caller.sessions.register("target-parent", None, "test", project_id=project.id)
    fallback = "allowed-backup" if allowed else "forbidden-backup"
    # In the caller's project scope-target has only an allowed fallback.
    _scope_definition(temp_db, project.id, "scope-target", [], fallback)
    _scope_definition(temp_db, project.id, fallback, [])
    monkeypatch.setattr(
        _factory,
        "_context_from_project_path",
        lambda path: {"id": project.id, "project_path": path},
    )
    launch = AsyncMock(return_value={"success": True, "run_id": "mock-launch"})
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    registry = _factory.create_spawn_agent_registry(
        MagicMock(),
        session_manager=spawn_caller.sessions,
        db=temp_db,
    )
    arguments: dict[str, Any] = {
        "prompt": "work",
        "agent": "scope-target",
        "parent_session_id": parent.id,
    }
    if source == "explicit-path":
        arguments["project_path"] = "/target"
        arguments["parent_session_id"] = spawn_caller.root.id
    with session_context_for_test(spawn_caller.child.id):
        result = await registry.call("spawn_agent", arguments)
    assert result["success"] is allowed
    if allowed:
        launch.assert_awaited_once()
        assert launch.await_args is not None
        assert launch.await_args.kwargs["target_project_id"] == project.id
    else:
        assert "spawnable_agents" in result["error"]
        launch.assert_not_awaited()


def test_validator_runs_excluded_from_active_count(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    project_id = str(sample_project["id"])
    parent = SessionManager(temp_db).register(
        external_id="validator-count-parent",
        machine_id=None,
        source="test",
        project_id=project_id,
    )
    runs = LocalAgentRunManager(temp_db)
    runs.create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="implement",
        agent_name="backend-developer",
    )
    runs.create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="validate close",
        agent_name=TASK_CLOSE_REVIEWER_AGENT,
    )

    assert _spawn_guards._count_active_agents(temp_db, project_id) == 1


@pytest.mark.asyncio
async def test_cap_error_reports_caller_owned_runs(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    project_id = str(sample_project["id"])
    sessions = SessionManager(temp_db)
    caller = sessions.register(
        external_id="cap-caller",
        machine_id=None,
        source="test",
        project_id=project_id,
    )
    other = sessions.register(
        external_id="cap-other",
        machine_id=None,
        source="test",
        project_id=project_id,
    )
    runs = LocalAgentRunManager(temp_db)
    for prompt in ("one", "two", "three"):
        runs.create(
            parent_session_id=caller.id,
            provider="codex",
            prompt=prompt,
            agent_name="backend-developer",
        )
    runs.create(
        parent_session_id=other.id,
        provider="codex",
        prompt="other",
        agent_name="backend-developer",
    )
    monkeypatch.setattr(_spawn_guards, "max_active_agents_for_project", lambda _path: 4)

    with session_context_for_test(caller.id):
        async with _spawn_guards.reserve_agent_slot(
            db=temp_db,
            project_id=project_id,
            project_path="/tmp/cap-error",
        ) as response:
            assert response is not None

    assert response["error"] == (
        "max_active_agents cap reached (4/4); 3 of these were spawned by this session"
    )


@pytest.mark.asyncio
async def test_reserve_agent_slot_counts_active_agents_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calling_thread = threading.current_thread()
    count_threads: list[threading.Thread] = []

    def count_active_agents(_db: object, _project_id: str) -> int:
        count_threads.append(threading.current_thread())
        return 0

    monkeypatch.setattr(_spawn_guards, "_count_active_agents", count_active_agents)
    monkeypatch.setattr(_spawn_guards, "max_active_agents_for_project", lambda _path: 1)

    async with _spawn_guards.reserve_agent_slot(
        db=object(),
        project_id="project-off-thread-count",
        project_path="/tmp/project-off-thread-count",
    ) as response:
        assert response is None

    assert count_threads
    assert count_threads[0] is not calling_thread


@pytest.mark.asyncio
async def test_reserve_agent_slot_cancellation_waits_then_cleans_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    committed = threading.Event()
    cleaned = asyncio.Event()

    def blocking_refusal(*_args: object, **_kwargs: object) -> None:
        entered.set()
        assert release.wait(timeout=5)
        committed.set()

    async def cleanup() -> None:
        assert committed.is_set()
        cleaned.set()

    monkeypatch.setattr(_spawn_guards, "agent_slot_cap_refusal", blocking_refusal)

    async def reserve() -> None:
        async with _spawn_guards.reserve_agent_slot(
            db=object(),
            project_id="project-cancelled-slot",
            project_path="/tmp/project-cancelled-slot",
            on_entry_cancel=cleanup,
        ):
            pytest.fail("cancelled entry must not yield a slot")

    task = asyncio.create_task(reserve())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


def _claim_step_agent(first_step: str = "claim") -> AgentDefinitionBody:
    return make_agent_definition(
        name="planner",
        prompts={"agent": "Claim the assigned task."},
        step_workflow=AgentStepWorkflowBody(steps=[WorkflowStep(name=first_step)]),
    )


@pytest.mark.asyncio
async def test_task_bound_claim_step_refuses_launch_without_a_task() -> None:
    """A claim-first agent cannot launch with neither task_id nor assigned_task_id."""
    context = await resolve_spawn_task_context(
        prompt="draft the plan",
        task_id=None,
        task_manager=None,
        project_id="project",
        allow_closed_task=False,
        agent_body=_claim_step_agent(),
        initial_variables=None,
    )

    assert context.refusal is not None
    assert context.refusal["success"] is False
    assert "assigned task" in context.refusal["error"]


@pytest.mark.asyncio
async def test_task_bound_claim_step_accepts_assigned_task_id() -> None:
    context = await resolve_spawn_task_context(
        prompt="draft the plan",
        task_id=None,
        task_manager=None,
        project_id="project",
        allow_closed_task=False,
        agent_body=_claim_step_agent(),
        initial_variables={"assigned_task_id": "#22784"},
    )

    assert context.refusal is None


@pytest.mark.asyncio
async def test_agent_without_claim_step_launches_without_a_task() -> None:
    context = await resolve_spawn_task_context(
        prompt="look around",
        task_id=None,
        task_manager=None,
        project_id="project",
        allow_closed_task=False,
        agent_body=_claim_step_agent("explore"),
        initial_variables=None,
    )

    assert context.refusal is None


def test_task_spawn_lease_releases_mutex_when_enter_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class ExplodingMutex:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            events.append("init")

        def __enter__(self) -> object:
            events.append("enter")
            raise RuntimeError("post-acquire failure")

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            _traceback: object,
        ) -> Literal[False]:
            events.append(("exit", exc_type, str(exc)))
            return False

    monkeypatch.setattr("gobby.dispatch.mutex.RuntimeDispatchMutex", ExplodingMutex)
    monkeypatch.setattr(_spawn_guards, "TaskDispatchMutexManager", lambda _db: object())

    lease = _spawn_guards.TaskSpawnLease(db=object(), task_id="task-1")

    with pytest.raises(RuntimeError, match="post-acquire failure"):
        lease.acquire()

    assert events == ["init", "enter", ("exit", RuntimeError, "post-acquire failure")]
    assert lease._mutex is None
    assert lease._owns_mutex is False
