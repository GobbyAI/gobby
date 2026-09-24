"""Human-readable labels for agents in Telegram controls and messages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from gobby.storage.session_models import Session


def agent_label(session: Session) -> str:
    """Prefer a role or title while removing an automatic session-ref prefix."""
    title = session.title.strip() if isinstance(session.title, str) else ""
    prefix, separator, remainder = title.partition(": ")
    if separator and prefix.rsplit("#", maxsplit=1)[-1].isdigit():
        title = remainder.strip()
    if title and not title.rsplit("#", maxsplit=1)[-1].isdigit():
        return title
    return session.source.replace("_", " ").title()


def agent_menu_labels(sessions: Sequence[Session]) -> dict[str, str]:
    """Give Telegram buttons distinct, bounded labels across all menu pages."""
    names = {session.id: agent_label(session) for session in sessions}
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
