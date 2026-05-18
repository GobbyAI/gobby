"""Composed agent run storage manager."""

from __future__ import annotations

from gobby.storage.database import DatabaseProtocol

from ._cleanup import _AgentRunCleanupMixin
from ._lifecycle import _AgentRunLifecycleMixin
from ._queries import _AgentRunQueryMixin
from ._runtime import _AgentRunRuntimeMixin
from ._selectors import _AgentRunSelectorMixin


class LocalAgentRunManager(
    _AgentRunSelectorMixin,
    _AgentRunLifecycleMixin,
    _AgentRunRuntimeMixin,
    _AgentRunQueryMixin,
    _AgentRunCleanupMixin,
):
    """Manager for agent run storage operations."""

    db: DatabaseProtocol

    def __init__(self, db: DatabaseProtocol):
        """Initialize with database connection."""
        self.db = db
