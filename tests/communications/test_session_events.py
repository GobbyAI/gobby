"""Tests for semantic session status communications events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from gobby.communications.models import CommsMessage
from gobby.communications.session_events import (
    format_session_status_message,
    route_session_status_transition,
    session_status_event_type,
)
from gobby.sessions.status_events import SessionStatusTransition


class RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_event(
        self,
        event_type: str,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
        *,
        event_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> list[CommsMessage]:
        self.calls.append(
            {
                "event_type": event_type,
                "content": content,
                "project_id": project_id,
                "session_id": session_id,
                "event_id": event_id,
                "metadata": metadata,
            }
        )
        return []


def make_transition(
    *,
    agent_run_id: str | None,
    status: str,
    transitioned_at: datetime | None = None,
) -> SessionStatusTransition:
    return SessionStatusTransition(
        session_id="11111111-1111-4111-8111-111111111111",
        project_id="22222222-2222-4222-8222-222222222222",
        agent_run_id=agent_run_id,
        status=status,
        transitioned_at=transitioned_at or datetime(2026, 7, 30, 23, 0, tzinfo=UTC),
        seq_num=42,
        title="Index docs",
        source="codex",
    )


@pytest.mark.parametrize(
    ("agent_run_id", "status", "expected"),
    [
        ("agent-run-1", "paused", "session.agent.paused"),
        ("agent-run-1", "expired", "session.agent.expired"),
        (None, "paused", "session.interactive.paused"),
        (None, "expired", "session.interactive.expired"),
    ],
)
def test_session_status_event_type_classifies_session_kind(
    agent_run_id: str | None,
    status: str,
    expected: str,
) -> None:
    assert (
        session_status_event_type(make_transition(agent_run_id=agent_run_id, status=status))
        == expected
    )


async def test_route_session_status_transition_preserves_scope_and_utc_event_id() -> None:
    router = RecordingRouter()
    central_time = datetime(
        2026,
        7,
        30,
        18,
        0,
        tzinfo=timezone(-timedelta(hours=5)),
    )
    transition = make_transition(
        agent_run_id="agent-run-1",
        status="paused",
        transitioned_at=central_time,
    )

    await route_session_status_transition(router, transition)

    assert router.calls == [
        {
            "event_type": "session.agent.paused",
            "content": "#42 - Index docs - Paused",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "event_id": ("11111111-1111-4111-8111-111111111111:paused:2026-07-30T23:00:00+00:00"),
            "metadata": None,
        }
    ]


async def test_route_session_status_transition_ignores_unpublished_status() -> None:
    router = RecordingRouter()
    transition = make_transition(agent_run_id=None, status="active")

    result = await route_session_status_transition(router, transition)

    assert result == []
    assert router.calls == []


def test_format_session_status_message_uses_session_fallback_without_title() -> None:
    transition = make_transition(agent_run_id=None, status="expired")
    transition = SessionStatusTransition(
        session_id=transition.session_id,
        project_id=transition.project_id,
        agent_run_id=transition.agent_run_id,
        status=transition.status,
        transitioned_at=transition.transitioned_at,
        seq_num=None,
        title=None,
        source=transition.source,
    )

    assert format_session_status_message(transition) == "Session - Expired"


@pytest.mark.parametrize(
    "legacy_title",
    ["#42 Codex", "#42 - Codex", "#42: Codex"],
)
def test_format_session_status_message_does_not_duplicate_legacy_ref(
    legacy_title: str,
) -> None:
    transition = make_transition(agent_run_id=None, status="paused")
    transition = SessionStatusTransition(
        session_id=transition.session_id,
        project_id=transition.project_id,
        agent_run_id=transition.agent_run_id,
        status=transition.status,
        transitioned_at=transition.transitioned_at,
        seq_num=42,
        title=legacy_title,
        source=transition.source,
    )

    assert format_session_status_message(transition) == "#42 - Codex - Paused"
