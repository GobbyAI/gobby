"""A compact handoff that fails to deliver leaves a blocked attention entry until a delivery lands."""

from __future__ import annotations

import pytest

from gobby.hooks.terminal_handoff_delivery import _compensate_delivery_failure
from gobby.sessions.compact_failure_attention import (
    COMPACT_FAILURE_ATTENTION_REASON,
    clear_compact_failure_attention,
    compact_failure_attention_manager,
)
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    ClaimedHandoffDelivery,
    claim_staged_handoff_delivery,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.attention import AttentionState, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000001"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
SESSION = "41000000-0000-4000-8000-000000000011"
ATTEMPT_ID = SESSION.replace("-", "")


def _session(hub_db: HubDatabase) -> None:
    if hub_db.fetchone("SELECT 1 FROM projects WHERE id = %s", (PROJECT_ID,)) is None:
        hub_db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "attention"))
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, session_type, "
        "status) VALUES (%s, %s, %s, 'codex', %s, 'terminal', 'active')",
        (SESSION, SESSION, MACHINE_ID, PROJECT_ID),
    )


def _claim(hub_db: HubDatabase) -> ClaimedHandoffDelivery:
    stage_handoff_attempt(
        hub_db,
        SESSION,
        attempt_id=ATTEMPT_ID,
        handoff=build_handoff_payload(current_state="working", next_steps=["continue"]),
        clear_session=False,
    )
    SessionVariableManager(hub_db).merge_variables(
        SESSION,
        {
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": ATTEMPT_ID,
                "clear_session": False,
            }
        },
    )
    claimed = claim_staged_handoff_delivery(hub_db, SESSION, ATTEMPT_ID)
    assert claimed is not None
    return claimed


def _blocked(hub_db: HubDatabase) -> AttentionState | None:
    return compact_failure_attention_manager(hub_db).get(session_attention_entry_id(SESSION))


@pytest.mark.parametrize("error_code", ["compact_unconfirmed", "compact_failed"])
def test_compact_failure_raises_a_blocked_attention_entry(
    hub_db: HubDatabase, error_code: str
) -> None:
    _session(hub_db)
    claimed = _claim(hub_db)

    _compensate_delivery_failure(
        hub_db,
        claimed,
        "compact boundary was not observed before the confirmation deadline",
        error_code=error_code,
    )

    entry = _blocked(hub_db)
    assert entry is not None
    assert (entry.state, entry.reason, entry.session_id) == (
        "blocked",
        COMPACT_FAILURE_ATTENTION_REASON,
        SESSION,
    )


def test_ordinary_delivery_failure_raises_no_compact_attention(hub_db: HubDatabase) -> None:
    _session(hub_db)
    claimed = _claim(hub_db)

    _compensate_delivery_failure(hub_db, claimed, "delivery timed out")

    assert _blocked(hub_db) is None


def test_clear_settles_the_compact_failure_entry(hub_db: HubDatabase) -> None:
    _session(hub_db)
    claimed = _claim(hub_db)
    _compensate_delivery_failure(
        hub_db, claimed, "compact boundary was not observed", error_code="compact_unconfirmed"
    )

    clear_compact_failure_attention(hub_db, SESSION)

    cleared = _blocked(hub_db)
    assert cleared is None or cleared.state is None


def test_clear_leaves_an_unrelated_blocked_entry_alone(hub_db: HubDatabase) -> None:
    _session(hub_db)
    compact_failure_attention_manager(hub_db).transition(
        session_attention_entry_id(SESSION),
        state="blocked",
        session_id=SESSION,
        reason="other_reason",
        kind="actionable",
        fingerprint="other-reason",
        payload={},
    )

    clear_compact_failure_attention(hub_db, SESSION)

    entry = _blocked(hub_db)
    assert entry is not None
    assert (entry.state, entry.reason) == ("blocked", "other_reason")
