"""Human-readable labels for agents in Telegram controls and messages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from gobby.sessions.display_labels import provider_display_name
from gobby.storage.session_models import Session


def agent_label(session: Session, seat_label: str | None = None) -> str:
    """Name a seat by its definition, else by its provider. Sessions store no title."""
    if seat_label:
        return seat_label
    return provider_display_name(session.source)


def agent_menu_labels(
    sessions: Sequence[Session], seat_labels: Mapping[str, str | None] | None = None
) -> dict[str, str]:
    """Give Telegram buttons distinct, bounded labels across all menu pages."""
    seats = seat_labels or {}
    names = {session.id: agent_label(session, seats.get(session.id)) for session in sessions}
    stems = {session_id: name[:42] for session_id, name in names.items()}
    counts = Counter(stem.casefold() for stem in stems.values())
    labels = {}
    for session in sessions:
        name = names[session.id]
        stem = stems[session.id]
        if counts[stem.casefold()] > 1 or len(name) > 46:
            seq_num = getattr(session, "seq_num", None)
            suffix = f" #{seq_num}" if isinstance(seq_num, int) else f" #{session.id[:8]}"
            labels[session.id] = f"{stem[: 46 - len(suffix)]}{suffix}"
        else:
            labels[session.id] = name[:46]
    return labels


def telegram_agent_label(session: Session | None, seat_label: str | None = None) -> str:
    """Identify seats or stable responder/system roles without exposing identity titles."""
    if session is None:
        return "System (gobby)"
    if session.source in {"comms", "web-chat", "web_chat"}:
        seat = "Responder"
    elif session.source == "system":
        seat = "System"
    else:
        seat = " ".join((seat_label or agent_label(session)).split())[:80]
    return f"{seat} ({session.ref})"
