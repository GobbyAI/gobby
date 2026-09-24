"""Human-readable labels for agents in Telegram controls and messages."""

from __future__ import annotations

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
