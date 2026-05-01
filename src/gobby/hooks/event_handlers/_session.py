"""Session event handler mixin — thin composition shim.

Re-exports the split session handler mixins as a single composed class
so that existing imports continue to work unchanged.
"""

from __future__ import annotations

from gobby.hooks.event_handlers._session_end import SessionEndMixin
from gobby.hooks.event_handlers._session_start import (
    SUMMARY_GENERATION_TIMEOUT_S,
    AgentActivationResult,
    SessionStartMixin,
)

__all__ = [
    "AgentActivationResult",
    "SUMMARY_GENERATION_TIMEOUT_S",
    "SessionEndMixin",
    "SessionEventHandlerMixin",
]


class SessionEventHandlerMixin(SessionStartMixin, SessionEndMixin):
    """Composed session event handler mixin."""

    pass
