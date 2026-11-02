"""The Reporter yields between ticks: the stop gate allows it and its cadence wait wakes the next tick."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.coordination_waits import CoordinationWaitService
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.engine.core import RuleEngine
from tests.events.test_coordination_waits import CoordinationHarness, connection_for
from tests.events.test_coordination_waits import harness as coordination_fixture
from tests.terminals.fakes import make_memory_terminal

pytestmark = pytest.mark.integration

harness = coordination_fixture

CADENCE_SECONDS = 1800


def _expire(h: CoordinationHarness, wait_id: str) -> None:
    h.db.execute(
        "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (wait_id,),
    )


@pytest.mark.parametrize("state", ["no_wait", "waiting", "woke"])
@pytest.mark.parametrize("allow_tick_yield", [False, True])
async def test_reporter_stop_gate_allows_the_yield_without_end_agent_run(
    harness: CoordinationHarness, state: str, allow_tick_yield: bool
) -> None:
    RuleDefinitionManager(harness.db).create(
        "fixture-standing-yield",
        {
            "event": "stop",
            "when": "variables.get('is_spawned_agent') and not variables.get('allow_tick_yield')",
            "effects": [{"type": "block", "reason": "Finish with end_agent_run"}],
        },
        tags=["default"],
    )
    if state != "no_wait":
        row = harness.wait(reply=True, timeout=CADENCE_SECONDS)
        if state == "woke":
            _expire(harness, row["id"])
    variables: dict[str, Any] = {
        "_memory_initial_stop_checked": True,
        "is_spawned_agent": True,
        "allow_tick_yield": allow_tick_yield,
    }
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id=harness.waiter,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
    )
    engine = RuleEngine(harness.db, task_manager=LocalTaskManager(harness.db))
    decision = await engine.evaluate(event, session_id=harness.waiter, variables=variables)

    assert decision.decision == ("allow" if allow_tick_yield else "block")
    assert ("end_agent_run" in (decision.reason or "")) is not allow_tick_yield


async def test_reporter_cadence_wait_wakes_next_tick_and_rearms(
    harness: CoordinationHarness,
) -> None:
    timeout = CADENCE_SECONDS
    harness.db.execute(
        "UPDATE sessions SET source = 'codex', status = 'paused' WHERE id = %s",
        (harness.waiter,),
    )
    LocalAgentRunManager(harness.db).create(
        parent_session_id=harness.owner,
        child_session_id=harness.waiter,
        provider="codex",
        agent_name="fixture-cadence-seat",
        prompt="Report on each policy cadence",
        resume_metadata_json={"execution_mode": "interactive", "provider_cli_mode": "terminal"},
    )
    terminal = replace(make_memory_terminal(backend="native"), session_id=harness.waiter)
    terminals = MagicMock()
    terminals.resolve_live_for_session.return_value = terminal
    sender = AsyncMock(return_value=utc_now())
    dispatcher = WakeDispatcher(
        session_manager=SessionManager(harness.db),
        ism_manager=harness.messages,
        terminal_manager=terminals,
        tmux_sender=sender,
    )
    service = CoordinationWaitService(
        harness.manager,
        CompletionEventRegistry(wake_callback=dispatcher.wake),
        require_machine_id(),
        lambda: connection_for(harness),
    )

    tick = harness.wait(reply=True, timeout=timeout)
    assert tick["outcome"] == "waiting"
    assert (tick["expires_at"] - datetime.now(UTC)).total_seconds() > CADENCE_SECONDS - 60

    _expire(harness, tick["id"])
    await service.process(tick["id"])
    woke = harness.row(tick["id"])
    assert woke["id"] == tick["id"]
    assert woke["outcome"] == "timeout"
    assert woke["delivered_at"] is not None
    sender.assert_awaited_once_with(
        terminal.id,
        CONTINUE_WAKE_MESSAGE,
        submit=True,
        clear_before_submit=True,
        cli_source="codex",
    )

    next_tick = harness.wait(reply=True, timeout=timeout)
    assert next_tick["id"] != tick["id"]
    assert next_tick["outcome"] == "waiting"
