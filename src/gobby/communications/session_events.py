"""Route committed session status transitions through communications."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.utils.datetime import datetime_to_required_iso

if TYPE_CHECKING:
    from gobby.communications.models import CommsMessage


class SessionEventRouter(Protocol):
    """Communications surface used by session status notifications."""

    async def send_event(
        self,
        event_type: str,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
        *,
        event_id: str | None = None,
    ) -> list[CommsMessage]: ...


def session_status_event_type(transition: SessionStatusTransition) -> str | None:
    """Return the public event type for a routable status transition."""
    if transition.status not in {"paused", "expired"}:
        return None
    session_kind = "agent" if transition.agent_run_id else "interactive"
    return f"session.{session_kind}.{transition.status}"


def format_session_status_message(transition: SessionStatusTransition) -> str:
    """Format concise user-facing transition content."""
    reference = f"#{transition.seq_num}" if transition.seq_num else transition.session_id[:8]
    title = transition.title.strip() if transition.title else ""
    subject = f"{title} ({reference})" if title else reference
    return (
        f"Session {transition.status}: {subject}\n"
        f"Provider: {transition.source}\n"
        f"Session ID: {transition.session_id}"
    )


async def route_session_status_transition(
    router: SessionEventRouter,
    transition: SessionStatusTransition,
) -> list[CommsMessage]:
    """Route one committed transition with a deterministic source event ID."""
    event_type = session_status_event_type(transition)
    if event_type is None:
        return []
    event_id = (
        f"{transition.session_id}:{transition.status}:"
        f"{datetime_to_required_iso(transition.transitioned_at)}"
    )
    return await router.send_event(
        event_type,
        format_session_status_message(transition),
        project_id=transition.project_id,
        session_id=transition.session_id,
        event_id=event_id,
    )
