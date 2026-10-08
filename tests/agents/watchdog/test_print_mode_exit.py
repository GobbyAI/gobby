"""A one-shot CLI without SessionEnd completes on a clean exit (#23778)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.sessions.turn_lifecycle import TurnDisposition, TurnEvidence, TurnLifecycleReducer
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.agents.test_lifecycle_monitor import (
    DETECTION_REGISTRY,
    LOCAL_MACHINE_ID,
    LifecycleRuntime,
    _fake_terminal_services,
    _make_terminal_run,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

ANSWER = "#### Command 4\nPROBE_FILE_GONE"


def _exited_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    *,
    provider: str,
    disposition: TurnDisposition | None,
) -> tuple[AgentLifecycleMonitor, LocalAgentRunManager, AgentRun]:
    """A spawned run whose child answered, ended its turn as given, and exited."""
    parent = session_manager.register(
        external_id=f"print-parent-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    child = session_manager.register(
        external_id=f"print-child-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source=provider,
        project_id=project_id,
        parent_session_id=parent.id,
        agent_depth=1,
    )
    runs = LocalAgentRunManager(temp_db)
    services = _fake_terminal_services(temp_db, LifecycleRuntime(backend="native"))
    run = _make_terminal_run(
        runs,
        parent.to_dict(),
        run_id=str(uuid4()),
        child_session_id=child.id,
        provider=provider,
        backend="native",
    )
    assert run.terminal_id is not None
    session_manager.update_stats(
        child.id, turn_count=1, tool_call_count=4, last_assistant_content=ANSWER
    )
    lifecycle = TurnLifecycleReducer(session_manager)
    lifecycle.begin_turn(child.id, TurnEvidence(source=provider))
    if disposition is not None:
        lifecycle.end_turn(child.id, disposition, TurnEvidence(source=provider, generation=1))

    completion = CompletionEventRegistry()
    coordinator = SessionCoordinator(agent_run_manager=runs)
    coordinator.set_completion_registry(completion)
    monitor = AgentLifecycleMonitor(
        agent_run_manager=runs,
        db=temp_db,
        detection_registry=DETECTION_REGISTRY,
        session_manager=session_manager,
        completion_registry=completion,
        terminal_services=services,
    )
    monitor.set_session_coordinator(coordinator)
    assert services.manager.mark_exited(run.terminal_id) is not None
    return monitor, runs, run


@pytest.mark.asyncio
async def test_clean_print_exit_completes_with_the_answer(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)
    monitor, runs, run = _exited_run(
        temp_db,
        session_manager,
        sample_project["id"],
        provider="agy",
        disposition="completed",
    )

    assert await monitor.check_unhealthy_agents() == 1

    stored = runs.get(run.id)
    assert stored is not None
    assert stored.status == "success"
    assert stored.result == ANSWER
    assert stored.error is None
    assert await monitor.check_unhealthy_agents() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "disposition", "reason"),
    [
        pytest.param("agy", None, "terminal session died unexpectedly", id="agy-died-mid-turn"),
        pytest.param(
            "agy",
            "ended_non_user",
            "agent exited after its last turn ended without completing (ended_non_user)",
            id="agy-error-stop",
        ),
        # SessionEnd stays the clean-exit authority for a CLI that has the hook.
        pytest.param(
            "droid", "completed", "terminal session died unexpectedly", id="droid-no-session-end"
        ),
    ],
)
async def test_exit_without_a_completed_last_turn_still_fails(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    disposition: TurnDisposition | None,
    reason: str,
) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)
    monitor, runs, run = _exited_run(
        temp_db,
        session_manager,
        sample_project["id"],
        provider=provider,
        disposition=disposition,
    )

    assert await monitor.check_unhealthy_agents() == 1

    stored = runs.get(run.id)
    assert stored is not None
    assert stored.status == "error"
    assert stored.error is not None
    assert stored.error.startswith(reason)
    assert await monitor.check_unhealthy_agents() == 0
