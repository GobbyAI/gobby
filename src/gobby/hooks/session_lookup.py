"""Session lookup and resolution service.

SessionLookupService encapsulates the logic for resolving platform session IDs
from CLI external IDs and enriching events with task context.
Extracted from HookManager.handle() as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.hooks.events import HookEvent, HookEventType
from gobby.hooks.project_context import apply_project_id_to_event, resolve_hook_project_context
from gobby.hooks.session_types import HookSessionManager
from gobby.hooks.terminal_context import enrich_terminal_context_with_cwd, hook_cwd
from gobby.tasks.state_semantics import serialize_task_state
from gobby.workflows.summary_actions import schedule_tmux_window_rename

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.session_models import Session
    from gobby.storage.session_tasks import SessionTaskManager


def _task_state_label(task: object) -> str:
    state = serialize_task_state(task)
    if state["is_closed"]:
        return "closed"
    if state["is_escalated"]:
        return "escalated"
    current_stage = state["current_stage"]
    return current_stage["state"] if current_stage else "ready"


class SessionLookupService:
    """Resolves platform session IDs and enriches events with session context.

    Handles:
    - Cache lookup via SessionManager
    - Database fallback with locking via SessionCoordinator
    - Auto-registration of unknown non-terminal sessions
    - Active task context enrichment
    """

    def __init__(
        self,
        session_manager: HookSessionManager,
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
        3. Auto-registration if not found for non-terminal events

        Also enriches the event with active task context and stores
        the platform session ID in event metadata.

        Args:
            event: HookEvent with session_id (external_id) and source

        Returns:
            Platform session ID or None if no external_id
        """
        explicit_platform_session_id, explicit_session = self._resolve_metadata_platform_session(
            event
        )
        explicit_project_id = getattr(explicit_session, "project_id", None)
        if isinstance(explicit_project_id, str) and explicit_project_id:
            apply_project_id_to_event(event, explicit_project_id)

        # Always resolve project_id, even if no session_id — downstream
        # code (_resolve_session_refs_in_tool_input) needs it for #N lookups.
        if not event.project_id:
            project_resolution = resolve_hook_project_context(
                event,
                session_manager=self._session_manager,
                resolve_project_id=self._resolve_project_id,
                logger=self._logger,
            )
            if project_resolution.skipped:
                self._logger.debug(
                    "Skipping session lookup without project context: %s",
                    project_resolution.reason,
                )
                return None

        if explicit_platform_session_id:
            self._revive_expired_terminal_session(explicit_platform_session_id, event)
            self._backfill_terminal_context(explicit_platform_session_id, event)
            self._enrich_task_context(explicit_platform_session_id, event)
            event.metadata["_platform_session_id"] = explicit_platform_session_id
            return explicit_platform_session_id

        external_id = event.session_id
        if not external_id:
            return None

        platform_session_id = self._resolve_session_id(external_id, event)

        # Resolve active task for this session
        if platform_session_id:
            self._revive_expired_terminal_session(platform_session_id, event)
            self._backfill_terminal_context(platform_session_id, event)
            self._enrich_task_context(platform_session_id, event)

        # Store platform session_id in event metadata for handlers
        event.metadata["_platform_session_id"] = platform_session_id

        return platform_session_id

    def _revive_expired_terminal_session(
        self,
        platform_session_id: str,
        event: HookEvent,
    ) -> None:
        """Repair false-expired terminal rows when a non-end hook arrives."""
        if event.event_type == HookEventType.SESSION_END:
            return

        try:
            self._session_manager.revive_expired_terminal_session(platform_session_id)
        except Exception as exc:
            self._logger.debug(
                "Failed to revive expired terminal session %s on %s: %s",
                platform_session_id,
                event.event_type.value,
                exc,
                exc_info=True,
            )
            return

    def _resolve_metadata_platform_session(
        self,
        event: HookEvent,
    ) -> tuple[str | None, Session | None]:
        """Return valid platform session metadata already supplied by hook ingress."""
        platform_session_id = event.metadata.get("_platform_session_id")
        if not isinstance(platform_session_id, str) or not platform_session_id:
            return None, None

        try:
            session = self._session_manager.get(platform_session_id)
        except Exception as exc:
            self._logger.debug(
                "Failed to validate platform session metadata %s: %s",
                platform_session_id,
                exc,
            )
            event.metadata.pop("_platform_session_id", None)
            return None, None

        if session is None:
            self._logger.debug("Ignoring unknown platform session metadata %s", platform_session_id)
            event.metadata.pop("_platform_session_id", None)
            return None, None

        return platform_session_id, session

    def _backfill_terminal_context(self, platform_session_id: str, event: HookEvent) -> None:
        """Merge terminal metadata discovered after the original registration."""
        raw_context = event.data.get("terminal_context")
        terminal_context = raw_context if isinstance(raw_context, dict) else None
        terminal_context = enrich_terminal_context_with_cwd(
            terminal_context,
            hook_cwd(event.data, event.cwd),
        )
        if not terminal_context:
            return

        try:
            updated_session, tmux_pane_added = self._session_manager.backfill_terminal_context(
                platform_session_id,
                terminal_context,
            )
        except Exception as exc:
            self._logger.debug(
                "Failed to backfill terminal context for session %s: %s",
                platform_session_id,
                exc,
            )
            return

        if tmux_pane_added and updated_session is not None:
            title = getattr(updated_session, "title", None) or ""
            schedule_tmux_window_rename(
                updated_session,
                title,
                loop=getattr(self._session_coordinator, "_event_loop", None),
            )

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
                    project_id = event.project_id

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
                        recovered_session = self._session_manager.recover_session(
                            external_id=external_id,
                            source=event.source.value,
                            machine_id=machine_id,
                            project_id=project_id,
                        )
                        if recovered_session:
                            platform_session_id = recovered_session.id
                            self._logger.warning(
                                "Recovered session %s for external_id=%s across source mismatch "
                                "(incoming=%s, existing=%s)",
                                platform_session_id,
                                external_id,
                                event.source.value,
                                recovered_session.source,
                            )
                            return platform_session_id

                        if event.event_type == HookEventType.SESSION_END:
                            self._logger.warning(
                                "Skipping auto-registration for orphaned SESSION_END: "
                                "external_id=%s not found in DB "
                                "(machine_id=%s, project_id=%s, source=%s).",
                                external_id,
                                machine_id,
                                project_id,
                                event.source.value,
                            )
                            return None

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
                            transcript_path=event.data.get("transcript_path"),
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
                    "state": _task_state_label(task),
                }
                # Keep legacy field for backwards compatibility
                event.metadata["_task_title"] = task.title
        except Exception as e:
            self._logger.warning(f"Failed to resolve active task: {e}")
