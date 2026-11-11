"""Record the model a hook payload reports onto its session (#25918).

Hooks report the live model before the transcript does: Antigravity names it on every
hook (``modelName``), Claude on SessionStart and PostModelSwitch, Codex on its prompt
and model-request hooks. The transcript processor keeps writing the model it parses,
so the two converge on the latest model either way.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from gobby.hooks.events import HookEvent

logger = logging.getLogger(__name__)


def reported_model(provider: str, value: object) -> str | None:
    """Return the canonical model a payload reports, or None when it names no model.

    The provider catalog resolves an alias (``opus``) to its canonical model. A name
    the catalog does not know is kept only when it reads as a model id: every model
    id has a hyphen (``claude-opus-5-5``, ``gemini-3-flash-agent``), and no bare
    alias does (``opus``, ``opusplan``, ``default``), so an unresolved alias is never
    stored as the model.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    model = value.strip()
    from gobby.agents.reasoning import _get_capability_resolver

    try:
        capability = _get_capability_resolver().find_model(provider, model)
    except (psycopg.Error, LookupError, TypeError, ValueError) as exc:
        logger.debug("Model catalog lookup failed for %s %s: %s", provider, model, exc)
        capability = None
    if capability is not None:
        return capability.canonical_model
    return model if "-" in model else None


def record_reported_model(handler: Any, event: HookEvent, value: object) -> str | None:
    """Write the payload's model onto the event's session; return what was written.

    A native subagent's hook binds to its parent's session, and the subagent's model
    is not the parent's, so it is never written there.
    """
    session_id = event.metadata.get("_platform_session_id")
    session_manager = getattr(handler, "_session_manager", None)
    if not session_id or session_manager is None or event.metadata.get("_native_subagent_binding"):
        return None
    model = reported_model(event.source.value, value)
    if model is None:
        return None
    try:
        session_manager.update(session_id, model=model)
    except (psycopg.Error, KeyError, TypeError, ValueError) as exc:
        logger.warning("Failed to record model %s for session %s: %s", model, session_id, exc)
        return None
    return model
