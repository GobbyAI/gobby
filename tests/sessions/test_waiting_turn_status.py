"""Turn-end status must distinguish durable work from an intentional interrupt."""

from typing import Any

import pytest

from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration


def test_completed_turn_with_armed_wait_is_not_paused(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    waiter = sessions.register(
        external_id="waiting-seat", machine_id=None, source="codex", project_id=sample_project["id"]
    )
    owner = sessions.register(
        external_id="wait-owner", machine_id=None, source="codex", project_id=sample_project["id"]
    )
    lifecycle = TurnLifecycleReducer(sessions)
    evidence = TurnEvidence(source="codex", provider_turn_key="waiting-turn")
    lifecycle.begin_turn(waiter.id, evidence)
    waits = CoordinationWaitManager(temp_db)
    waits.register(waiter.id, owner.id, reply=True)
    assert waits.has_active_wait(waiter.id)

    ended = lifecycle.end_turn(waiter.id, "completed", evidence)

    assert ended.applied
    assert waits.has_active_wait(waiter.id)
    stored = sessions.get(waiter.id)
    assert stored is not None
    assert stored.status == "waiting", (
        "A completed turn with an armed coordination wait has no operator-pause intent"
    )


def test_user_interrupt_retains_explicit_interrupt_status(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    seat = sessions.register(
        external_id="interrupted-seat",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    lifecycle = TurnLifecycleReducer(sessions)
    evidence = TurnEvidence(source="codex", provider_turn_key="interrupted-turn")
    lifecycle.begin_turn(seat.id, evidence)

    ended = lifecycle.end_turn(seat.id, "user_interrupted", evidence)

    assert ended.applied
    stored = sessions.get(seat.id)
    assert stored is not None
    assert stored.status == "interrupted"
