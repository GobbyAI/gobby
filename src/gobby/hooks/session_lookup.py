"""Session lookup and resolution service.

SessionLookupService encapsulates the logic for resolving platform session IDs
from CLI external IDs and enriching events with task context.
Extracted from HookManager.handle() as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.hooks.events import HookEvent, HookEventType

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.sessions.manager import SessionManager
    from gobby.storage.session_tasks import SessionTaskManager


class SessionLookupService:
    """Resolves platform session IDs and enriches events with session context.

    Handles:
    - Cache lookup via SessionManager
    - Database fallback with locking via SessionCoordinator
    - Auto-registration of unknown sessions
    - Active task context enrichment
    """

    def __init__(
        self,
        session_manager: SessionManager,
        session_coordinator: SessionCoordinator,
        session_task_manager: SessionTaskManager,
        get_machine_id: Callable[[], str],
        resolve_project_id: Callable[[str | None, str | None], str],
        logger: logging.Logger,
    ):
        self._session_manager = session_manager
        self._session_coordinator = session_coordinator
        self._session_task_manager = session_task_manager
        self._get_machine_id = get_machine_id
        self._resolve_project_id = resolve_project_id
        self._logger = logger

    def resolve(self, event: HookEvent) -> str | None:
        """Resolve platform session ID from event and enrich with task context.

        Looks up the platform session ID from the CLI's external_id via:
        1. SessionManager cache
        2. Database lookup with locking
        3. Auto-registration if not found

        Also enriches the event with active task context and stores
        the platform session ID in event metadata.

        Args:
            event: HookEvent with session_id (external_id) and source

        Returns:
            Platform session ID or None if no external_id
        """
        external_id = event.session_id
        if not external_id:
            return None

        platform_session_id = self._resolve_session_id(external_id, event)

        # Resolve active task for this session
        if platform_session_id:
            self._enrich_task_context(platform_session_id, event)

        # Store platform session_id in event metadata for handlers
        event.metadata["_platform_session_id"] = platform_session_id

        return platform_session_id

    def _resolve_session_id(self, external_id: str, event: HookEvent) -> str | None:
        """Look up or create platform session ID for the given external_id."""
        # Check SessionManager's cache first (keyed by (external_id, source))
        platform_session_id = self._session_manager.get_session_id(external_id, event.source.value)

        # If not in mapping and not session-start, try to query database
        if not platform_session_id and event.event_type != HookEventType.SESSION_START:
            with self._session_coordinator.get_lookup_lock():
                # Double check in case another thread finished lookup
                platform_session_id = self._session_manager.get_session_id(
                    external_id, event.source.value
                )

                if not platform_session_id:
                    self._logger.debug(
                        f"Session not in mapping, querying database for external_id={external_id}"
                    )
                    # Resolve context for lookup
                    machine_id = event.machine_id or self._get_machine_id()
                    cwd = event.data.get("cwd")
                    project_id = self._resolve_project_id(event.data.get("project_id"), cwd)

                    # Lookup with full composite key
                    platform_session_id = self._session_manager.lookup_session_id(
                        external_id,
                        source=event.source.value,
                        machine_id=machine_id,
                        project_id=project_id,
                    )
                    if platform_session_id:
                        self._logger.debug(
                            f"Found session_id {platform_session_id} for external_id {external_id}"
                        )
                    else:
                        # Auto-register session if not found
                        self._logger.warning(
                            "Session auto-registration: external_id=%s not found in DB "
                            "(machine_id=%s, project_id=%s, source=%s). Creating new session.",
                            external_id,
                            machine_id,
                            project_id,
                            event.source.value,
                        )
                        platform_session_id = self._session_manager.register_session(
                            external_id=external_id,
                            machine_id=machine_id,
                            project_id=project_id,
                            parent_session_id=None,
                            jsonl_path=event.data.get("transcript_path"),
                            source=event.source.value,
                            project_path=cwd,
                            terminal_context=event.data.get("terminal_context"),
                        )

        return platform_session_id

    def _enrich_task_context(self, platform_session_id: str, event: HookEvent) -> None:
        """Add active task context to event metadata."""
        try:
            # Get tasks linked with 'worked_on' action which implies active focus
            session_tasks = self._session_task_manager.get_session_tasks(platform_session_id)
            # Filter for active 'worked_on' tasks - taking the most recent one
            active_tasks = [t for t in session_tasks if t.get("action") == "worked_on"]
            if active_tasks:
                # Use the most recent task - populate full task context
                task = active_tasks[0]["task"]
                event.task_id = task.id
                event.metadata["_task_context"] = {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                }
                # Keep legacy field for backwards compatibility
                event.metadata["_task_title"] = task.title
        except Exception as e:
            self._logger.warning(f"Failed to resolve active task: {e}")
