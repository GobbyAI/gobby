"""Route committed session status transitions through communications."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol

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
        metadata: dict[str, Any] | None = None,
    ) -> list[CommsMessage]: ...


def session_status_event_type(transition: SessionStatusTransition) -> str | None:
    """Return the public event type for a routable status transition."""
    if transition.status not in {"paused", "expired"}:
        return None
    session_kind = "agent" if transition.agent_run_id else "interactive"
    return f"session.{session_kind}.{transition.status}"


def format_session_status_message(
    transition: SessionStatusTransition,
    *,
    label: str | None = None,
    assistant_message: str | None = None,
) -> str:
    """Format concise user-facing transition content."""
    title = _canonical_session_title(transition)
    status_label = label or transition.status.title()
    content = f"{title} - {status_label}"
    if assistant_message:
        content = f"{content}\n\n{assistant_message}"
    return content


def _canonical_session_title(transition: SessionStatusTransition) -> str:
    title = transition.title.strip() if transition.title else "Session"
    if transition.seq_num is None:
        return title
    legacy_prefix = re.compile(rf"^#{transition.seq_num}(?:\s*[-–—:]\s*|\s+)")
    title = legacy_prefix.sub("", title, count=1).strip() or "Session"
    return f"#{transition.seq_num} - {title}"


async def route_session_status_transition(
    router: SessionEventRouter,
    transition: SessionStatusTransition,
    *,
    label: str | None = None,
    assistant_message: str | None = None,
    event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[CommsMessage]:
    """Route one committed transition with a deterministic source event ID."""
    event_type = session_status_event_type(transition)
    if event_type is None:
        return []
    source_event_id = event_id or (
        f"{transition.session_id}:{transition.status}:"
        f"{datetime_to_required_iso(transition.transitioned_at)}"
    )
    return await router.send_event(
        event_type,
        format_session_status_message(
            transition,
            label=label,
            assistant_message=assistant_message,
        ),
        project_id=transition.project_id,
        session_id=transition.session_id,
        event_id=source_event_id,
        metadata=metadata,
    )
