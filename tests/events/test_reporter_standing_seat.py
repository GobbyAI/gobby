"""The Reporter yields between ticks: the stop gate allows it and its cadence wait wakes the next tick."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.events.test_coordination_waits import CoordinationHarness
from tests.events.test_coordination_waits import harness as coordination_fixture

pytestmark = pytest.mark.integration

harness = coordination_fixture

REPORTER = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents/reporter.yaml"
)
CADENCE_SECONDS = 1800


def _cadence_wait_timeout() -> int:
    """Read the wait timeout the reporter prompt renders for the policy cadence."""
    data: dict[str, Any] = yaml.safe_load(REPORTER.read_text(encoding="utf-8"))
    prompt = AgentDefinitionBody.model_validate(data).prompt_for(
        "agent",
        variables={"project_policy": {"roots": [], "report_cadence_seconds": CADENCE_SECONDS}},
    )
    match = re.search(r"wait_for_coordination\(reply=true, timeout=(\d+)\)", prompt)
    assert match is not None
    return int(match.group(1))


def _expire(h: CoordinationHarness, wait_id: str) -> None:
    h.db.execute(
        "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (wait_id,),
    )


@pytest.mark.parametrize("state", ["no_wait", "waiting", "woke"])
async def test_reporter_stop_gate_allows_the_yield_without_end_agent_run(
    harness: CoordinationHarness, state: str
) -> None:
    sync_bundled_rules(harness.db, get_bundled_rules_path())
    if state != "no_wait":
        row = harness.wait(reply=True, timeout=_cadence_wait_timeout())
        if state == "woke":
            _expire(harness, row["id"])
    variables: dict[str, Any] = {
        "_memory_initial_stop_checked": True,
        "is_spawned_agent": True,
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

    assert decision.decision == "allow"
    assert "end_agent_run" not in (decision.reason or "")


async def test_reporter_cadence_wait_wakes_next_tick_and_rearms(
    harness: CoordinationHarness,
) -> None:
    timeout = _cadence_wait_timeout()
    assert timeout == CADENCE_SECONDS

    tick = harness.wait(reply=True, timeout=timeout)
    assert tick["outcome"] == "waiting"
    assert (tick["expires_at"] - datetime.now(UTC)).total_seconds() > CADENCE_SECONDS - 60

    _expire(harness, tick["id"])
    woke = harness.wait(reply=True, timeout=timeout)
    assert woke["id"] == tick["id"]
    assert woke["outcome"] == "timeout"

    next_tick = harness.wait(reply=True, timeout=timeout)
    assert next_tick["id"] != tick["id"]
    assert next_tick["outcome"] == "waiting"
