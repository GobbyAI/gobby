"""Team progress reads each task's stage spans and the stall flags its Dev Manager watches.

Stage spans come from the claim's recorded hand-offs, so a span ends where the next hand-off
begins and the last span ends at close. The flags name what a monitoring loop must act on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.team_progress import (
    HANDOFF_BOUND_SECONDS,
    TaskProgress,
    TeamProgress,
    team_progress,
)
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, make_team_world
from tests.workflows.test_claim_handoff import (
    Chain,
    _candidate,
    _chain,
    _hand,
    _implement,
    _record,
    _send,
    _verdict,
)

pytestmark = pytest.mark.unit

# Real time, three hours back, so the stamped spans are in the past and later records sort after.
START = datetime.now(UTC) - timedelta(hours=3)


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    return make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )


def _at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def _pin_hand_offs(chain: Chain, offsets: list[float]) -> None:
    """Stamp the claim's hand-off events, in order, and its first claim row, at the offsets."""
    events = sorted(
        (
            event
            for event in TaskLifecycleEventManager(chain.db).list_lifecycle_events(chain.task.id)
            if event.to_state == "claim:handed_off"
        ),
        key=lambda event: event.id,
    )
    assert len(events) == len(offsets)
    for event, offset in zip(events, offsets, strict=True):
        chain.db.execute(
            "UPDATE task_lifecycle_events SET created_at = %s WHERE id = %s",
            (_at(offset), event.id),
        )
    chain.db.execute(
        "UPDATE session_tasks SET created_at = %s WHERE task_id = %s AND action = 'claimed'",
        (_at(0), chain.task.id),
    )


def _progress(chain: Chain, now: float | datetime) -> TaskProgress:
    moment = now if isinstance(now, datetime) else _at(now)
    result: TeamProgress = team_progress(chain.db, chain.team.binding.team_id, now=moment)
    found = [item for item in result.tasks if item.task_id == chain.task.id]
    assert found, f"team progress lists no task for claim {chain.task.id}"
    return found[0]


async def _handed_to_reviewer(world: TeamWorld, title: str) -> Chain:
    """The writer's test CANDIDATE hands the claim to the reviewer at +600 seconds."""
    chain = await _chain(world, title)
    _record(chain, chain.writer, "test", chain.tests_sha)
    ids = _send(
        chain,
        chain.writer,
        _candidate(chain, chain.tests_sha),
        chain.manager,
        chain.reviewer,
    )
    _hand(chain, chain.writer, "review", ids)
    _pin_hand_offs(chain, [600])
    return chain


async def test_stage_spans_and_claim_to_close_come_from_the_hand_offs(world: TeamWorld) -> None:
    chain = await _handed_to_reviewer(world, "Stage spans follow the claim")
    _record(chain, chain.reviewer, "review", chain.tests_sha)
    ids = _send(chain, chain.reviewer, _verdict(chain, chain.tests_sha, "LAND"), chain.developer)
    _hand(chain, chain.reviewer, "next_open", ids)
    _implement(chain)
    _record(chain, chain.developer, "implementation", chain.impl_sha)
    ids = _send(chain, chain.developer, _candidate(chain, chain.impl_sha), chain.reviewer)
    _hand(chain, chain.developer, "review", ids)
    _record(chain, chain.reviewer, "review", chain.impl_sha)
    ids = _send(
        chain,
        chain.reviewer,
        _verdict(chain, chain.impl_sha, "LAND"),
        chain.manager,
        chain.developer,
    )
    _hand(chain, chain.reviewer, "next_open", ids)
    _pin_hand_offs(chain, [600, 1800, 3000, 4200])
    closed = TaskLifecycleEventManager(chain.db).record_lifecycle_event(
        chain.task.id, None, "closed", "done", by_actor="close_task"
    )
    chain.db.execute(
        "UPDATE task_lifecycle_events SET created_at = %s WHERE id = %s",
        (_at(4800), closed.id),
    )

    task = _progress(chain, now=4800)

    assert [(span.stage, span.seconds) for span in task.stages] == [
        ("test", 600.0),
        ("review", 1200.0),
        ("implementation", 1200.0),
        ("review", 1200.0),
        ("closing", 600.0),
    ]
    assert task.claim_to_close_seconds == 4800.0
    assert task.flags == frozenset()


async def test_hand_off_unacknowledged_past_the_bound_is_flagged(world: TeamWorld) -> None:
    chain = await _handed_to_reviewer(world, "Unacknowledged hand-off")

    task = _progress(chain, now=600 + HANDOFF_BOUND_SECONDS + 1)

    assert "handoff_unacknowledged" in task.flags


async def test_hand_off_acknowledged_by_the_new_holder_is_not_flagged(world: TeamWorld) -> None:
    chain = await _handed_to_reviewer(world, "Acknowledged hand-off")
    # The reviewer records a review step after the hand-off, which acknowledges it.
    _record(chain, chain.reviewer, "review", chain.tests_sha, status="rejected")

    task = _progress(chain, now=600 + HANDOFF_BOUND_SECONDS + 1)

    assert "handoff_unacknowledged" not in task.flags


async def test_holder_without_a_live_seat_is_flagged(world: TeamWorld) -> None:
    chain = await _handed_to_reviewer(world, "Holder without a live seat")
    chain.db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (chain.reviewer,))

    task = _progress(chain, now=700)

    assert "holder_not_live" in task.flags


async def test_idle_claim_on_open_work_is_flagged(world: TeamWorld) -> None:
    chain = await _chain(world, "Idle claim on open work")
    _pin_hand_offs(chain, [])

    task = _progress(chain, now=HANDOFF_BOUND_SECONDS + 1)

    assert "idle_on_open_work" in task.flags
    assert "handoff_unacknowledged" not in task.flags


async def test_pending_hand_off_is_flagged_for_the_dev_manager(world: TeamWorld) -> None:
    chain = await _chain(world, "Pending hand-off is flagged")
    _implement(chain)
    LocalTaskManager(chain.db).claim_task(chain.task.id, chain.developer, force=True)
    chain.db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (chain.reviewer,))
    ids = _send(chain, chain.developer, _candidate(chain, chain.impl_sha), chain.reviewer)
    _hand(chain, chain.developer, "review", ids)

    task = _progress(chain, now=60)

    assert "handoff_pending" in task.flags


def test_team_progress_is_a_registered_task_tool(temp_db: HubDatabase) -> None:
    tools = create_task_registry(LocalTaskManager(temp_db))

    assert "team_progress" in tools
