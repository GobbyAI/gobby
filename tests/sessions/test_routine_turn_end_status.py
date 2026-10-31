"""A routine turn end is idle, never held; only an operator pause or interrupt is held."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake_active_recovery import (
    reconcile_idle_prompt_session,
    reconcile_restart_stale_session,
)
from gobby.sessions.turn_lifecycle import TurnDisposition, TurnEvidence, TurnLifecycleReducer
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration


def test_routine_completed_turn_without_a_wait_is_idle(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    seat = sessions.register(
        external_id="routine-seat",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    lifecycle = TurnLifecycleReducer(sessions)
    evidence = TurnEvidence(source="codex", provider_turn_key="routine-turn")
    lifecycle.begin_turn(seat.id, evidence)

    ended = lifecycle.end_turn(seat.id, "completed", evidence)

    assert ended.applied
    assert CoordinationWaitManager(temp_db).has_active_wait(seat.id) is False
    stored = sessions.get(seat.id)
    assert stored is not None
    assert stored.status == "idle", "A turn that ended with nothing armed is idle, not held"


def test_non_user_turn_end_without_a_wait_is_idle(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    seat = sessions.register(
        external_id="background-seat",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    lifecycle = TurnLifecycleReducer(sessions)
    evidence = TurnEvidence(source="codex", provider_turn_key="background-turn")
    lifecycle.begin_turn(seat.id, evidence)

    ended = lifecycle.end_turn(seat.id, "ended_non_user", evidence)

    assert ended.applied
    stored = sessions.get(seat.id)
    assert stored is not None
    assert stored.status == "idle"


def test_paused_subscription_stores_only_paused(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    waiter = sessions.register(
        external_id="subscriber-seat",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    owner = sessions.register(
        external_id="subscribed-owner",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )

    row = CoordinationWaitManager(temp_db).register(waiter.id, owner.id, statuses=["paused"])

    assert json.loads(row["statuses"]) == ["paused"]


@pytest.mark.parametrize("disposition", ["completed", "ended_non_user"])
def test_operator_pause_survives_an_unarmed_routine_stop(
    temp_db: HubDatabase, sample_project: dict[str, Any], disposition: TurnDisposition
) -> None:
    sessions = SessionManager(temp_db)
    seat = sessions.register(
        external_id=f"paused-unarmed-{disposition}",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    lifecycle = TurnLifecycleReducer(sessions)
    evidence = TurnEvidence(source="codex", provider_turn_key=f"paused-unarmed-{disposition}")
    lifecycle.begin_turn(seat.id, evidence)
    sessions.update_session_status(seat.id, "paused")

    ended = lifecycle.end_turn(seat.id, disposition, evidence)

    assert ended.applied
    stored = sessions.get(seat.id)
    assert stored is not None
    assert stored.status == "paused", "An operator pause holds through a matching routine stop"


@pytest.mark.parametrize("disposition", ["completed", "ended_non_user"])
def test_operator_pause_survives_an_armed_routine_stop(
    temp_db: HubDatabase, sample_project: dict[str, Any], disposition: TurnDisposition
) -> None:
    sessions = SessionManager(temp_db)
    seat = sessions.register(
        external_id=f"paused-armed-{disposition}",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    owner = sessions.register(
        external_id=f"paused-armed-owner-{disposition}",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    lifecycle = TurnLifecycleReducer(sessions)
    evidence = TurnEvidence(source="codex", provider_turn_key=f"paused-armed-{disposition}")
    lifecycle.begin_turn(seat.id, evidence)
    CoordinationWaitManager(temp_db).register(seat.id, owner.id, statuses=["paused"])
    sessions.update_session_status(seat.id, "paused")

    ended = lifecycle.end_turn(seat.id, disposition, evidence)

    assert ended.applied
    stored = sessions.get(seat.id)
    assert stored is not None
    assert stored.status == "paused", "An operator pause holds even while a wait is armed"


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["completed", "ended_non_user"])
@pytest.mark.parametrize("armed", [False, True], ids=["unarmed", "armed"])
@pytest.mark.parametrize("recovery", ["restart-sweep", "idle-prompt", "inactive-sweep"])
async def test_recovery_then_routine_stop_never_holds(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    disposition: TurnDisposition,
    armed: bool,
    recovery: str,
) -> None:
    sessions = SessionManager(temp_db)
    seat = sessions.register(
        external_id=f"swept-{armed}-{disposition}",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    lifecycle = TurnLifecycleReducer(sessions)
    evidence = TurnEvidence(source="codex", provider_turn_key="pre-restart-turn")
    lifecycle.begin_turn(seat.id, evidence)
    if armed:
        owner = sessions.register(
            external_id=f"swept-owner-{disposition}",
            machine_id=None,
            source="codex",
            project_id=sample_project["id"],
        )
        CoordinationWaitManager(temp_db).register(seat.id, owner.id, statuses=["idle"])

    horizon = datetime.now(UTC)
    before = horizon - timedelta(minutes=31)
    temp_db.execute(
        "UPDATE sessions SET updated_at = %s, last_activity = %s WHERE id = %s",
        (before, before, seat.id),
    )
    observed = sessions.get(seat.id)
    assert observed is not None

    async def empty_activity(_session: object, _terminal: object | None) -> TerminalActivity:
        return TerminalActivity(ComposerRead("empty"))

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    if recovery == "restart-sweep":
        reconciled = await reconcile_restart_stale_session(
            session_manager=sessions,
            observed=observed,
            terminal=None,
            activity_probe=empty_activity,
            run_db=run_db,
            restart_horizon_ms=int(horizon.timestamp() * 1_000),
            excluded_session_ids=frozenset(),
        )
    elif recovery == "idle-prompt":
        await reconcile_idle_prompt_session(
            session_manager=sessions,
            observed=observed,
            terminal=None,
            activity_probe=empty_activity,
            run_db=run_db,
        )
        reconciled = sessions.get(seat.id)
    else:
        assert sessions.idle_inactive_active_sessions(timeout_minutes=30) == 1
        reconciled = sessions.get(seat.id)
    assert reconciled is not None
    ended = lifecycle.end_turn(seat.id, disposition, evidence)

    assert ended.applied
    stored = sessions.get(seat.id)
    assert stored is not None
    assert stored.status == ("waiting" if armed else "idle")
    assert reconciled.status == "idle", "Recovery carries no operator pause intent"
    assert reconciled.last_activity == before, "Recovery preserves the activity clock"
