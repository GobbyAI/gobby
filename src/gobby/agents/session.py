"""
Child session management for subagent spawning.

This module provides utilities for creating and managing child sessions
that are spawned by agents. Child sessions are linked to their parent
sessions and track agent depth for safety limits.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import LocalSessionManager

logger = logging.getLogger(__name__)


@dataclass
class ChildSessionConfig:
    """Configuration for creating a child session."""

    parent_session_id: str
    """ID of the parent session spawning this child."""

    project_id: str
    """Project ID the session belongs to."""

    machine_id: str
    """Machine identifier."""

    source: str
    """CLI source (e.g., 'claude', 'gemini', 'codex')."""

    agent_id: str | None = None
    """ID of the agent that spawned this session."""

    workflow_name: str | None = None
    """Name of the workflow being executed."""

    title: str | None = None
    """Optional session title."""

    git_branch: str | None = None
    """Git branch for the session."""

    external_id: str | None = None
    """External session ID (e.g., Gemini's session_id from preflight capture)."""

    lifecycle_variables: dict[str, Any] | None = None
    """Lifecycle variables for the session."""


class ChildSessionManager:
    """
    Manages child session creation and lifecycle.

    Child sessions are spawned by agents running in parent sessions.
    They track:
    - Parent-child relationships via parent_session_id
    - Agent depth (0 = human-initiated, 1+ = agent-spawned)
    - Which agent spawned them (for tracking and cleanup)

    Thread-safe: Uses the underlying LocalSessionManager's thread safety.
    """

    def __init__(
        self,
        session_storage: LocalSessionManager,
        max_agent_depth: int = 1,
    ) -> None:
        """
        Initialize ChildSessionManager.

        Args:
            session_storage: LocalSessionManager for SQLite operations.
            max_agent_depth: Maximum allowed nesting depth (default: 1).
                Depth 0 = human-initiated session.
                Depth 1 = agent can spawn, but child cannot spawn further.
        """
        self._storage = session_storage
        self.max_agent_depth = max_agent_depth
        self.logger = logger

    def get_session_depth(self, session_id: str) -> int:
        """
        Get the agent depth of a session.

        Uses the stored agent_depth field set at session creation:
        - 0: Human-initiated or pipeline session
        - 1+: Agent-spawned session (parent_depth + 1)

        Args:
            session_id: The session ID to check.

        Returns:
            Agent depth (0 for human sessions, 1+ for agent sessions).
        """
        session = self._storage.get(session_id)
        if not session:
            return 0
        return session.agent_depth or 0

    def can_spawn_child(self, parent_session_id: str) -> tuple[bool, str, int]:
        """
        Check if a session can spawn a child agent.

        Args:
            parent_session_id: The session attempting to spawn.

        Returns:
            Tuple of (can_spawn, reason, parent_depth).
            The parent_depth is returned to avoid redundant depth lookups.
        """
        parent = self._storage.get(parent_session_id)
        if not parent:
            return False, f"Parent session {parent_session_id} not found", 0

        current_depth = self.get_session_depth(parent_session_id)
        if current_depth >= self.max_agent_depth:
            return (
                False,
                (
                    f"Max agent depth ({self.max_agent_depth}) exceeded. "
                    f"Current depth: {current_depth}"
                ),
                current_depth,
            )

        return True, "OK", current_depth

    def create_child_session(
        self,
        config: ChildSessionConfig,
    ) -> Session:
        """
        Create a child session linked to a parent.

        Args:
            config: Configuration for the child session.

        Returns:
            The created child Session.

        Raises:
            ValueError: If max_agent_depth would be exceeded.
        """
        # Check depth limit (also returns parent_depth to avoid redundant lookup)
        can_spawn, reason, parent_depth = self.can_spawn_child(config.parent_session_id)
        if not can_spawn:
            raise ValueError(reason)

        # Calculate child's agent depth (parent depth + 1)
        child_depth = parent_depth + 1

        # Use provided external_id (e.g., from Gemini preflight) or generate placeholder
        if config.external_id:
            external_id = config.external_id
            use_provided_external_id = True
        else:
            external_id = f"agent-{uuid.uuid4().hex[:12]}"
            use_provided_external_id = False

        # Create title if not provided
        title = config.title
        if not title:
            if config.workflow_name:
                title = f"Agent: {config.workflow_name}"
            else:
                title = "Agent session"

        # Register the child session
        child = self._storage.register(
            external_id=external_id,
            machine_id=config.machine_id,
            source=config.source,
            project_id=config.project_id,
            title=title,
            git_branch=config.git_branch,
            parent_session_id=config.parent_session_id,
            agent_depth=child_depth,
            spawned_by_agent_id=config.agent_id,
            workflow_name=config.workflow_name,
        )

        child_id = child.id

        # For sessions with provided external_id (e.g., Gemini preflight), keep it.
        # For sessions without (e.g., Claude with --session-id), update external_id
        # to match internal id so session_start hook can find this pre-created session.
        if not use_provided_external_id:
            self._storage.update(session_id=child_id, external_id=child_id)
        # Re-fetch to get updated external_id
        updated_child = self._storage.get(child_id)
        if updated_child is None:
            raise RuntimeError(f"Failed to fetch child session {child_id} after creation")

        self.logger.info(
            f"Created child session {updated_child.id} "
            f"(parent={config.parent_session_id}, agent={config.agent_id})"
        )

        return updated_child

    def update_terminal_pickup_metadata(
        self,
        session_id: str,
        agent_run_id: str | None = None,
        workflow_name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Delegate to underlying storage for terminal pickup metadata updates."""
        return self._storage.update_terminal_pickup_metadata(
            session_id=session_id,
            agent_run_id=agent_run_id,
            workflow_name=workflow_name,
            **kwargs,
        )

    def get_child_sessions(self, parent_session_id: str) -> list[Session]:
        """
        Get all child sessions of a parent.

        Args:
            parent_session_id: The parent session ID.

        Returns:
            List of child Session objects.
        """
        return self._storage.find_children(parent_session_id)

    def get_session_lineage(self, session_id: str) -> list[Session]:
        """
        Get the full lineage of a session (from root to current).

        Args:
            session_id: The session to trace.

        Returns:
            List of sessions from root (human-initiated) to current.
        """
        lineage: list[Session] = []
        current_id: str | None = session_id

        while current_id:
            session = self._storage.get(current_id)
            if not session:
                break
            lineage.append(session)
            current_id = session.parent_session_id

            # Safety limit
            if len(lineage) > 10:
                self.logger.warning(f"Lineage exceeded safety limit for {session_id}")
                break

        # Reverse to get root-to-current order
        lineage.reverse()
        return lineage
