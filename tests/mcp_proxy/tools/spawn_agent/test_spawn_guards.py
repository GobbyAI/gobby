from __future__ import annotations

import threading

import pytest

from gobby.mcp_proxy.tools.spawn_agent import _spawn_guards
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import resolve_spawn_task_context
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
    WorkflowStep,
)
from tests.fixtures.agent_definitions import make_agent_definition


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
        ) -> bool:
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
