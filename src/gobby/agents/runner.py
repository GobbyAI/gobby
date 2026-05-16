"""
Agent runner for orchestrating agent execution.

The AgentRunner coordinates:
- Creating child sessions for agents
- Tracking agent runs in the database
- Managing running agent lifecycle
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.agents import runner_queries as _queries
from gobby.agents.session import ChildSessionManager
from gobby.storage.agents import AgentRun, LocalAgentRunManager

__all__ = ["AgentRunner"]

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.hooks import WorkflowHookHandler

logger = logging.getLogger(__name__)


class AgentRunner:
    """
    Manages agent session tracking, run records, and lifecycle queries.

    The runner:
    1. Checks spawn eligibility (depth limits)
    2. Tracks agent runs in the database
    3. Provides query access to run history and status
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        session_storage: SessionManager,
        max_agent_depth: int = 1,
    ):
        """
        Initialize AgentRunner.

        Args:
            db: Database connection.
            session_storage: Session storage manager.
            max_agent_depth: Maximum nesting depth for agents.
        """
        self.db = db
        self._session_manager = session_storage
        self._child_session_manager = ChildSessionManager(
            session_storage,
            max_agent_depth=max_agent_depth,
        )
        self._run_storage = LocalAgentRunManager(db)
        self.logger = logger

        # Workflow handler for hook evaluation on spawned agent tool calls
        self._workflow_handler: WorkflowHookHandler | None = None
        self.agent_lifecycle_monitor: Any | None = None

    @property
    def workflow_handler(self) -> WorkflowHookHandler | None:
        """Workflow handler for hook evaluation on spawned agent tool calls."""
        return self._workflow_handler

    @workflow_handler.setter
    def workflow_handler(self, value: WorkflowHookHandler | None) -> None:
        self._workflow_handler = value

    @property
    def child_session_manager(self) -> ChildSessionManager:
        """Public accessor for the child session manager."""
        return self._child_session_manager

    @property
    def run_storage(self) -> LocalAgentRunManager:
        """Public accessor for the agent run storage manager."""
        return self._run_storage

    def can_spawn(self, parent_session_id: str) -> tuple[bool, str, int]:
        """
        Check if an agent can be spawned from the given session.

        Args:
            parent_session_id: The session attempting to spawn.

        Returns:
            Tuple of (can_spawn, reason, parent_depth).
            The parent_depth is returned to avoid redundant depth lookups.
        """
        return self._child_session_manager.can_spawn_child(parent_session_id)

    def get_run(self, run_id: str) -> Any | None:
        """Get an agent run by ID. Delegates to runner_queries."""
        return _queries.get_run(self, run_id)

    def get_run_id_by_session(self, session_id: str) -> str | None:
        """Get agent run_id by child session_id. Delegates to runner_queries."""
        return _queries.get_run_id_by_session(self, session_id)

    def list_runs(
        self,
        parent_session_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Any]:
        """List agent runs for a session. Delegates to runner_queries."""
        return _queries.list_runs(self, parent_session_id, status=status, limit=limit)

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a running agent. Delegates to runner_queries."""
        return _queries.cancel_run(self, run_id)

    def complete_run(self, run_id: str, result: str | None = None) -> bool:
        """Complete a running agent (self-termination). Delegates to runner_queries."""
        return _queries.complete_run(self, run_id, result=result)

    # -------------------------------------------------------------------------
    # Running Agents Management (DB-driven)
    # -------------------------------------------------------------------------

    def get_running_agent(self, run_id: str) -> AgentRun | None:
        """Get a running agent by ID from DB."""
        run = self._run_storage.get(run_id)
        if run and run.status in ("running", "pending"):
            return run
        return None

    def get_running_agents(self, parent_session_id: str | None = None) -> list[AgentRun]:
        """Get all running agents from DB."""
        if parent_session_id:
            return self._run_storage.list_by_parent(parent_session_id)
        return self._run_storage.list_active()

    def get_running_agents_count(self) -> int:
        """Get count of running agents."""
        return len(self._run_storage.list_active())

    def is_agent_running(self, run_id: str) -> bool:
        """Check if an agent is running."""
        run = self._run_storage.get(run_id)
        return run is not None and run.status in ("running", "pending")
