"""Tests for provider-aware session turn lifecycle reduction."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def _session(
    manager: SessionManager,
    project_id: str,
    *,
    external_id: str = "lifecycle-session",
) -> str:
    return manager.register(
        external_id=external_id,
        machine_id=None,
        source="codex",
        project_id=project_id,
    ).id


def test_transition_matrix_and_simultaneous_wait_priority(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"])
    lifecycle = TurnLifecycleReducer(sessions)

    started = lifecycle.begin_turn(
        session_id,
        TurnEvidence(source="codex", provider_turn_key="turn-1"),
    )
    assert started.applied is True
    assert started.generation == 1
    assert started.status == "active"

    approval = lifecycle.enter_wait(
        session_id,
        kind="approval",
        token="approval-1",
        evidence=TurnEvidence(
            source="codex",
            generation=1,
            provider_turn_key="turn-1",
            request_id="request-1",
        ),
    )
    assert approval.status == "awaiting_approval"
    input_wait = lifecycle.enter_wait(
        session_id,
        kind="input",
        token="question-1",
        evidence=TurnEvidence(source="codex", generation=1, provider_turn_key="turn-1"),
    )
    assert input_wait.status == "awaiting_input"

    resolved_input = lifecycle.resolve_wait(
        session_id,
        token="question-1",
        resolution="resumed",
        evidence=TurnEvidence(source="codex", generation=1, provider_turn_key="turn-1"),
    )
    assert resolved_input.status == "awaiting_approval"
    resolving = lifecycle.resolve_wait(
        session_id,
        token="approval-1",
        resolution="ambiguous",
        evidence=TurnEvidence(source="codex", generation=1, provider_turn_key="turn-1"),
    )
    assert resolving.status == "awaiting_approval"
    assert resolving.lifecycle.waits[0].state == "resolving"

    resumed = lifecycle.resumed_work(
        session_id,
        TurnEvidence(source="codex", generation=1, provider_turn_key="turn-1"),
    )
    assert resumed.status == "active"
    completed = lifecycle.end_turn(
        session_id,
        "completed",
        TurnEvidence(source="codex", generation=1, provider_turn_key="turn-1"),
    )
    assert completed.status == "paused"


def test_replacement_prompt_fences_late_wait_and_terminal_evidence(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="replacement")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="grok", provider_turn_key="prompt-1"))
    lifecycle.enter_wait(
        session_id,
        kind="input",
        token="interaction-1",
        evidence=TurnEvidence(source="grok", generation=1, provider_turn_key="prompt-1"),
    )

    replacement = lifecycle.begin_turn(
        session_id,
        TurnEvidence(source="grok", provider_turn_key="prompt-2"),
    )
    assert replacement.generation == 2
    assert replacement.status == "active"
    assert replacement.lifecycle.waits == ()

    late_wait = lifecycle.resolve_wait(
        session_id,
        token="interaction-1",
        resolution="abandoned",
        evidence=TurnEvidence(source="grok", generation=1, provider_turn_key="prompt-1"),
    )
    late_end = lifecycle.end_turn(
        session_id,
        "user_interrupted",
        TurnEvidence(source="grok", generation=1, provider_turn_key="prompt-1"),
    )
    assert late_wait.applied is False
    assert late_wait.reason == "stale_generation"
    assert late_end.applied is False
    assert late_end.reason == "stale_generation"
    current = sessions.get(session_id)
    assert current is not None
    assert current.status == "active"


def test_duplicate_current_prompt_does_not_advance_generation_or_clear_wait(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="duplicate-prompt")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="grok", provider_turn_key="prompt-1"))
    lifecycle.enter_wait(
        session_id,
        kind="input",
        token="question-1",
        evidence=TurnEvidence(source="grok", generation=1, provider_turn_key="prompt-1"),
    )

    duplicate = lifecycle.begin_turn(
        session_id,
        TurnEvidence(source="grok", provider_turn_key="prompt-1"),
    )

    assert duplicate.applied is False
    assert duplicate.generation == 1
    assert duplicate.status == "awaiting_input"
    assert [wait.token for wait in duplicate.lifecycle.waits] == ["question-1"]


def test_notification_after_terminal_turn_cannot_create_a_new_wait(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="terminal-notification")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="claude"))
    lifecycle.end_turn(
        session_id,
        "completed",
        TurnEvidence(source="claude", generation=1),
    )

    delayed = lifecycle.enter_wait(
        session_id,
        kind="approval",
        token="late-permission",
        evidence=TurnEvidence(source="claude.notification", generation=1),
    )

    assert delayed.applied is False
    assert delayed.status == "paused"
    assert delayed.lifecycle.waits == ()


def test_unknown_duplicate_and_mismatched_evidence_preserve_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="unknown")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="claude"))
    first = lifecycle.enter_wait(
        session_id,
        kind="approval",
        token="permission-1",
        evidence=TurnEvidence(source="claude", generation=1),
    )
    duplicate = lifecycle.enter_wait(
        session_id,
        kind="approval",
        token="permission-1",
        evidence=TurnEvidence(source="claude", generation=1),
    )
    mismatch = lifecycle.resolve_wait(
        session_id,
        token="permission-other",
        resolution="resumed",
        evidence=TurnEvidence(source="claude", generation=1),
    )
    unknown = lifecycle.end_turn(
        session_id,
        "unknown",
        TurnEvidence(source="claude", generation=1),
    )

    assert first.status == "awaiting_approval"
    assert duplicate.applied is False
    assert mismatch.applied is False
    assert unknown.applied is False
    current = sessions.get(session_id)
    assert current is not None
    assert current.status == "awaiting_approval"


def test_resolved_wait_cannot_be_resurrected_by_delayed_notification(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="delayed-notification")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="claude"))
    lifecycle.enter_wait(
        session_id,
        kind="approval",
        token="permission-1",
        evidence=TurnEvidence(source="claude", generation=1, request_id="permission-1"),
    )
    lifecycle.resumed_work(session_id, TurnEvidence(source="claude", generation=1))

    delayed = lifecycle.enter_wait(
        session_id,
        kind="approval",
        token="permission-1",
        evidence=TurnEvidence(source="claude.notification", generation=1),
    )

    assert delayed.applied is False
    assert delayed.reason == "duplicate"
    updated = sessions.get(session_id)
    assert updated is not None
    assert updated.status == "active"


def test_user_interruption_requires_explicit_disposition(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="interrupt")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="qwen"))

    interrupted = lifecycle.end_turn(
        session_id,
        "user_interrupted",
        TurnEvidence(source="qwen", generation=1),
    )
    assert interrupted.status == "interrupted"

    replacement = lifecycle.begin_turn(session_id, TurnEvidence(source="qwen"))
    assert replacement.status == "active"


def test_attention_screen_updates_preserve_lifecycle_namespace(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="attention")
    attention = AttentionStateManager(temp_db, epoch="turn-lifecycle")
    lifecycle = TurnLifecycleReducer(sessions, attention)
    lifecycle.begin_turn(session_id, TurnEvidence(source="agy", provider_turn_key="turn-1"))
    entry_id = session_attention_entry_id(session_id)

    opened = attention.transition(
        entry_id,
        state="blocked",
        session_id=session_id,
        reason="approval",
        kind="actionable",
        fingerprint="screen-1",
        payload={"screen": {"title": "Approve?"}},
    )
    assert opened.current is not None
    assert opened.current.payload["turn_lifecycle"]
    assert opened.current.payload["screen"] == {"title": "Approve?"}

    updated = attention.transition(
        entry_id,
        state="blocked",
        session_id=session_id,
        reason="approval",
        kind="actionable",
        fingerprint="screen-2",
        payload={"dialog": {"title": "Confirm?"}},
    )
    assert updated.current is not None
    assert updated.current.payload["turn_lifecycle"]
    assert updated.current.payload["dialog"] == {"title": "Confirm?"}
    assert "screen" not in updated.current.payload

    cleared = attention.transition(entry_id, state=None)
    assert cleared.current is not None
    assert cleared.current.state is None
    assert set(cleared.current.payload) == {"turn_lifecycle"}


@pytest.mark.parametrize("terminal_status", ["expired", "deleted"])
@pytest.mark.parametrize("operation", ["resumed_work", "begin_turn"])
def test_delayed_work_cannot_reactivate_terminal_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    terminal_status: str,
    operation: str,
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"])
    lifecycle = TurnLifecycleReducer(sessions)
    started = lifecycle.begin_turn(session_id, TurnEvidence(source="codex"))
    sessions.update_status(session_id, terminal_status)

    result = getattr(lifecycle, operation)(session_id, TurnEvidence(source="codex"))

    assert result.applied is False
    assert result.reason == "session_terminal"
    assert result.lifecycle == started.lifecycle
    stored = sessions.get(session_id)
    assert stored is not None
    assert stored.status == terminal_status
