"""Tests for provider-aware session turn lifecycle reduction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake_active_recovery import reconcile_restart_stale_session
from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests._timing import drain_asyncio_tasks

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


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


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


@pytest.mark.asyncio
async def test_post_restart_genuine_turn_is_not_reconciled(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    sender_id = _session(sessions, sample_project["id"], external_id="restart-sender")
    recipient_id = _session(sessions, sample_project["id"], external_id="genuine-turn")
    lifecycle = TurnLifecycleReducer(sessions)
    horizon_ms = int((datetime.now(UTC) - timedelta(seconds=1)).timestamp() * 1_000)
    lifecycle.begin_turn(
        recipient_id,
        TurnEvidence(source="codex", provider_turn_key="post-restart-turn"),
    )
    observed = sessions.get(recipient_id)
    assert observed is not None

    async def empty_activity(_session: object, _terminal: object | None) -> TerminalActivity:
        return TerminalActivity(ComposerRead("empty"))

    reconciled = await reconcile_restart_stale_session(
        session_manager=sessions,
        observed=observed,
        terminal=None,
        activity_probe=empty_activity,
        run_db=_run_db,
        restart_horizon_ms=horizon_ms,
        excluded_session_ids=frozenset(),
    )
    assert reconciled is None
    active = sessions.get(recipient_id)
    assert active is not None and active.status == "active"

    messages = InterSessionMessageManager(temp_db)
    messages.create_message(
        from_session=sender_id,
        to_session=recipient_id,
        content="wait for real boundary",
        metadata_json='{"wake_requested": true}',
    )
    calls: list[str] = []

    class StatusDispatcher:
        async def dispatch_live_wake(
            self,
            session_id: str,
            *,
            priority: str = "normal",
        ) -> dict[str, Any]:
            current = sessions.get(session_id)
            assert current is not None
            calls.append(current.status)
            return {
                "session_id": session_id,
                "delivered": current.status == "paused",
                "method": "terminal" if current.status == "paused" else "next_call_context",
                "decline_reason": None if current.status == "paused" else "session_active",
            }

    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=sessions,
        dispatcher=StatusDispatcher(),
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())
    await coordinator.open()
    assert calls == ["active"]

    lifecycle.end_turn(
        recipient_id,
        "completed",
        TurnEvidence(source="codex", generation=1, provider_turn_key="post-restart-turn"),
    )
    await drain_asyncio_tasks(cycles=10)
    assert calls == ["active", "paused"]


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


def test_resumed_work_clears_resolving_pane_input_wait_when_turn_key_changes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A Codex goal tool hook clears pane input waits left in resolving."""
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="goal-working")
    lifecycle = TurnLifecycleReducer(sessions)
    started = lifecycle.begin_turn(
        session_id,
        TurnEvidence(source="codex", provider_turn_key="goal-turn"),
    )
    lifecycle.enter_wait(
        session_id,
        kind="input",
        token="composer-question",
        evidence=TurnEvidence(source="codex.pane", generation=started.generation),
    )
    resolved = lifecycle.resolve_wait(
        session_id,
        token="composer-question",
        resolution="ambiguous",
        evidence=TurnEvidence(source="pane.resolved", generation=started.generation),
    )
    assert resolved.status == "awaiting_input"
    assert resolved.lifecycle.waits[0].state == "resolving"

    resumed = lifecycle.resumed_work(
        session_id,
        TurnEvidence(source="codex", provider_turn_key="goal-turn-tool"),
    )

    assert resumed.applied is True
    assert resumed.status == "active"
    assert resumed.lifecycle.waits == ()
    current = sessions.get(session_id)
    assert current is not None
    assert current.status == "active"


def test_stale_provider_turn_rejection_is_logged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An open question is not cleared by a different turn key, and the reject is logged."""
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="real-question")
    lifecycle = TurnLifecycleReducer(sessions)
    started = lifecycle.begin_turn(
        session_id,
        TurnEvidence(source="codex", provider_turn_key="goal-turn"),
    )
    lifecycle.enter_wait(
        session_id,
        kind="input",
        token="real-question",
        evidence=TurnEvidence(source="codex.pane", generation=started.generation),
    )

    with caplog.at_level("DEBUG", logger="gobby.sessions.turn_lifecycle"):
        resumed = lifecycle.resumed_work(
            session_id,
            TurnEvidence(source="codex", provider_turn_key="other-turn"),
        )

    assert resumed.applied is False
    assert resumed.reason == "stale_provider_turn"
    current = sessions.get(session_id)
    assert current is not None
    assert current.status == "awaiting_input"
    assert any(
        record.levelname == "DEBUG" and "stale_provider_turn" in record.message
        for record in caplog.records
    )


def test_rejected_turn_end_is_logged_at_info(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale turn end leaves the row active, so the reject must reach INFO logs."""
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="stale-stop")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="codex", provider_turn_key="turn-1"))
    lifecycle.begin_turn(session_id, TurnEvidence(source="codex", provider_turn_key="turn-2"))

    with caplog.at_level("INFO", logger="gobby.sessions.turn_lifecycle"):
        ended = lifecycle.end_turn(
            session_id,
            "completed",
            TurnEvidence(source="codex", generation=1, provider_turn_key="turn-1"),
        )

    assert ended.applied is False
    assert ended.reason == "stale_generation"
    current = sessions.get(session_id)
    assert current is not None and current.status == "active"
    assert [record.getMessage() for record in caplog.records if record.levelname == "INFO"] == [
        f"Rejected completed turn end for session {session_id}: stale_generation "
        "(source=codex evidence_generation=1 generation=2 status=active)"
    ]


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
    lifecycle.begin_turn(session_id, TurnEvidence(source="agy"))

    interrupted = lifecycle.end_turn(
        session_id,
        "user_interrupted",
        TurnEvidence(source="agy", generation=1),
    )
    assert interrupted.status == "interrupted"

    replacement = lifecycle.begin_turn(session_id, TurnEvidence(source="agy"))
    assert replacement.status == "active"


def test_turn_end_disposition_stays_durable_until_the_next_turn(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="disposition")
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(session_id, TurnEvidence(source="agy"))
    assert lifecycle.get(session_id).disposition is None

    ended = lifecycle.end_turn(
        session_id, "ended_non_user", TurnEvidence(source="agy", generation=1)
    )
    assert ended.status == "paused"
    assert ended.lifecycle.disposition == "ended_non_user"
    # A fresh reader sees the stored payload, as the health monitor does after exit.
    assert TurnLifecycleReducer(sessions).get(session_id).disposition == "ended_non_user"

    lifecycle.begin_turn(session_id, TurnEvidence(source="agy"))
    assert lifecycle.get(session_id).disposition is None


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

    lifecycle.end_turn(
        session_id, "ended_non_user", TurnEvidence(source="agy", provider_turn_key="turn-1")
    )
    preserved = attention.get(entry_id)
    assert preserved is not None
    assert preserved.payload["dialog"] == {"title": "Confirm?"}

    cleared = attention.transition(entry_id, state=None)
    assert cleared.current is not None
    assert cleared.current.state is None
    assert set(cleared.current.payload) == {"turn_lifecycle"}


def test_provider_failure_count_persists_until_completed_turn(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="provider-retry")
    attention = AttentionStateManager(temp_db)
    lifecycle = TurnLifecycleReducer(sessions, attention)
    entry_id = session_attention_entry_id(session_id)

    for turn in range(1, 5):
        evidence = TurnEvidence(source="claude", provider_turn_key=f"turn-{turn}")
        lifecycle.begin_turn(session_id, evidence)
        failed = lifecycle.record_provider_failure(
            session_id,
            error_type="api_error",
            message="API Error: 500 Internal server error",
            retryable=True,
            max_resumes=3,
            evidence=evidence,
        )
        assert failed.applied
        assert failed.status == "paused"
        assert failed.lifecycle.provider_error is not None
        assert failed.lifecycle.provider_error.attempts == turn
        assert TurnLifecycleReducer(sessions, attention).get(session_id).provider_error == (
            failed.lifecycle.provider_error
        )
        duplicate = lifecycle.record_provider_failure(
            session_id,
            error_type="api_error",
            message="API Error: 500 Internal server error",
            retryable=True,
            max_resumes=3,
            evidence=evidence,
        )
        assert not duplicate.applied
        current = attention.get(entry_id)
        assert current is not None
        assert current.state == ("blocked" if turn == 4 else None)

    assert current is not None
    assert current.reason == "provider_error"
    assert current.payload["message"] == "API Error: 500 Internal server error"
    assert current.payload["error_type"] == "api_error"

    completed_evidence = TurnEvidence(source="claude", provider_turn_key="turn-5")
    lifecycle.begin_turn(session_id, completed_evidence)
    lifecycle.end_turn(session_id, "completed", completed_evidence)
    assert lifecycle.get(session_id).provider_error is None
    cleared = attention.get(entry_id)
    assert cleared is not None
    assert cleared.state is None


def test_nonretryable_provider_failure_blocks_without_resume(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    session_id = _session(sessions, sample_project["id"], external_id="provider-auth")
    attention = AttentionStateManager(temp_db)
    lifecycle = TurnLifecycleReducer(sessions, attention)
    evidence = TurnEvidence(source="claude", provider_turn_key="turn-1")
    lifecycle.begin_turn(session_id, evidence)
    failed = lifecycle.record_provider_failure(
        session_id,
        error_type="authentication_failed",
        message="API Error: 401 Invalid API key",
        retryable=False,
        max_resumes=3,
        evidence=evidence,
    )

    assert failed.lifecycle.provider_error is not None
    assert failed.lifecycle.provider_error.attempts == 1
    current = attention.get(session_attention_entry_id(session_id))
    assert current is not None
    assert current.state == "blocked"
    assert current.reason == "provider_error"
    assert current.payload["message"] == "API Error: 401 Invalid API key"


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
