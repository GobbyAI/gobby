"""A routine turn end is idle, never held; only an operator pause or interrupt is held."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
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


def test_paused_subscription_also_sees_idle_and_waiting_turn_ends(
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

    assert set(json.loads(row["statuses"])) == {"paused", "waiting", "idle"}
