"""Composed agent run storage manager."""

from __future__ import annotations

from gobby.sessions.status_events import SessionStatusTransitionCallback
from gobby.storage.hub.protocol import HubDatabase

from ._cleanup import _AgentRunCleanupMixin
from ._lifecycle import _AgentRunLifecycleMixin
from ._queries import _AgentRunQueryMixin
from ._runtime import _AgentRunRuntimeMixin
from ._selectors import _AgentRunSelectorMixin
from ._termination import _AgentRunTerminationMixin


class LocalAgentRunManager(
    _AgentRunSelectorMixin,
    _AgentRunLifecycleMixin,
    _AgentRunRuntimeMixin,
    _AgentRunTerminationMixin,
    _AgentRunQueryMixin,
    _AgentRunCleanupMixin,
):
    """Manager for agent run storage operations."""

    db: HubDatabase

    def __init__(
        self,
        db: HubDatabase,
        *,
        status_notifier: SessionStatusTransitionCallback | None = None,
    ):
        """Initialize with database connection."""
        self.db = db
        self._status_notifier = status_notifier
