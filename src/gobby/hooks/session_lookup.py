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
from gobby.hooks.terminal_context import (
    enrich_terminal_context_with_cwd,
    hook_cwd,
    is_gobby_acp_child,
)
from gobby.sessions.compact_identity import resolve_compact_continuation
from gobby.sessions.tmux_window_naming import schedule_tmux_window_rename
from gobby.storage.session_activity import reconcile_compact_session_activity
from gobby.storage.sessions._constants import TERMINAL_SESSION_STATUSES
from gobby.tasks.state_semantics import serialize_task_state

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


#: Hooks a CLI emits without user activity. A deferred SessionStart followed by
#: only these keeps ``sessions`` empty: Claude fires ``Setup``,
#: ``InstructionsLoaded``, and ``MessageDisplay`` at boot, before any prompt,
#: and none of them carries a context channel for the startup packet.
NON_MATERIALIZING_EVENTS: frozenset[HookEventType] = frozenset(
    {
        HookEventType.SESSION_END,
        HookEventType.NOTIFICATION,
        HookEventType.SETUP,
        HookEventType.INSTRUCTIONS_LOADED,
        HookEventType.CONFIG_CHANGE,
        HookEventType.CWD_CHANGED,
        HookEventType.FILE_CHANGED,
        HookEventType.DIRECTORY_ADDED,
        HookEventType.MESSAGE_DISPLAY,
        HookEventType.TEAMMATE_IDLE,
    }
)


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
        get_machine_id: Callable[[], str | None],
        resolve_project_id: Callable[[str | None, str | None], str],
        logger: logging.Logger,
    ):
        self._session_manager = session_manager
        self._session_coordinator = session_coordinator
        self._session_task_manager = session_task_manager
        self._get_machine_id = get_machine_id
        self._resolve_project_id = resolve_project_id
        self._logger = logger

    def resolve(
        self,
        event: HookEvent,
        *,
        apply_session_mutations: bool = True,
    ) -> str | None:
        """Resolve platform session ID from event and enrich with task context.

        Looks up the platform session ID from the CLI's external_id via:
        1. SessionManager cache
        2. Database lookup with locking
        3. Auto-registration if not found for non-terminal events

        Also enriches the event with active task context and stores
        the platform session ID in event metadata.

        Args:
            event: HookEvent with session_id (external_id) and source
            apply_session_mutations: When False, defer the session-mutating
                steps (terminal revive and terminal-context backfill) so the
                ingress identity fence can reject a stale hook before it
                writes onto the durable session; call
                :meth:`apply_session_mutations` after acceptance.

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
            if apply_session_mutations:
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
            if apply_session_mutations:
                self._revive_expired_terminal_session(platform_session_id, event)
                self._backfill_terminal_context(platform_session_id, event)
            self._enrich_task_context(platform_session_id, event)

        # Store platform session_id in event metadata for handlers. Never
        # store a null id: downstream consumers treat key presence as a
        # validated canonical session id.
        if platform_session_id:
            event.metadata["_platform_session_id"] = platform_session_id
        else:
            event.metadata.pop("_platform_session_id", None)

        return platform_session_id

    def apply_session_mutations(
        self,
        event: HookEvent,
        platform_session_id: str | None,
    ) -> None:
        """Apply revive/backfill deferred by ``resolve(apply_session_mutations=False)``.

        Called only after the ingress identity fence accepted the hook.
        """
        if not platform_session_id:
            return
        self._revive_expired_terminal_session(platform_session_id, event)
        self._backfill_terminal_context(platform_session_id, event)

    def validate_platform_session_metadata(self, event: HookEvent) -> str | None:
        """Validate caller-supplied _platform_session_id without side effects.

        Pops the key when it does not name a known session. Used on the
        SESSION_START path, which skips full resolve() so the handler can bind
        the real session without a premature auto-registration.
        """
        platform_session_id, _session = self._resolve_metadata_platform_session(event)
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
        machine_id = event.machine_id or self._get_machine_id()
        project_id = event.project_id
        platform_session_id = self._session_manager.get_session_id(
            external_id,
            event.source.value,
            project_id=project_id,
        )

        if platform_session_id or event.event_type == HookEventType.SESSION_START:
            return platform_session_id

        with self._session_coordinator.get_lookup_lock(external_id, event.source.value):
            platform_session_id = self._session_manager.get_session_id(
                external_id,
                event.source.value,
                project_id=project_id,
            )
            if platform_session_id:
                return platform_session_id

            return self._resolve_uncached_session_id(
                external_id,
                event,
                machine_id=machine_id,
                project_id=project_id,
            )

    def _resolve_uncached_session_id(
        self,
        external_id: str,
        event: HookEvent,
        *,
        machine_id: str | None,
        project_id: str | None,
    ) -> str | None:
        """Resolve a session after cache lookup has failed under the lookup lock."""
        self._logger.debug(
            "Session not in mapping, querying database for external_id=%s", external_id
        )
        platform_session_id = self._session_manager.lookup_session_id(
            external_id,
            source=event.source.value,
            project_id=project_id,
        )
        if platform_session_id:
            self._logger.debug(
                "Found session_id %s for external_id %s",
                platform_session_id,
                external_id,
            )
            return platform_session_id

        recovered_session = self._session_manager.recover_session(
            external_id=external_id,
            source=event.source.value,
            project_id=project_id,
        )
        if recovered_session:
            # recover_session relaxes two dimensions that lookup_session_id enforces:
            # it searches across sources, and unlike the cache validity check in
            # storage.sessions._registration_cache it does not exclude retired rows.
            # Seeing a retired row here is deliberate — a CLI still emitting events
            # after a handoff expired its session should rebind to that session
            # rather than accumulate a duplicate, which is the case
            # revive_expired_terminal_session then repairs. Report whichever
            # dimension actually differed: blaming source for a status-driven
            # recovery sends a same-source WARNING to errors.log on a routine path,
            # which buries the warnings that matter.
            if recovered_session.source != event.source.value:
                self._logger.warning(
                    "Recovered session %s for external_id=%s across source mismatch "
                    "(incoming=%s, existing=%s)",
                    recovered_session.id,
                    external_id,
                    event.source.value,
                    recovered_session.source,
                )
            elif recovered_session.status in TERMINAL_SESSION_STATUSES:
                self._logger.info(
                    "Recovered %s session %s for external_id=%s (source=%s); "
                    "the exact lookup skips retired rows",
                    recovered_session.status,
                    recovered_session.id,
                    external_id,
                    event.source.value,
                )
            else:
                self._logger.info(
                    "Recovered session %s for external_id=%s after the exact lookup "
                    "missed (source=%s, status=%s)",
                    recovered_session.id,
                    external_id,
                    event.source.value,
                    recovered_session.status,
                )
            return recovered_session.id

        compact_handled, compact_session_id = self._recover_compact_session(
            external_id,
            event,
            machine_id=machine_id,
            project_id=project_id,
        )
        if compact_handled:
            return compact_session_id

        if event.event_type in NON_MATERIALIZING_EVENTS:
            self._logger.info(
                "Skipping auto-registration for orphaned %s: "
                "external_id=%s not found in DB "
                "(machine_id=%s, project_id=%s, source=%s).",
                event.event_type.name,
                external_id,
                machine_id,
                project_id,
                event.source.value,
            )
            return None

        if is_gobby_acp_child(event.data.get("terminal_context")):
            # An ACP child shares its external_id with the web_chat session that
            # spawned it (#16002). Terminal-typed lookups above cannot see that
            # parent, so bind to it explicitly before giving up.
            web_chat_parent = self._session_manager.find_by_external_id(
                external_id,
                project_id,
                event.source.value,
                session_type="web_chat",
            )
            if web_chat_parent is not None:
                self._logger.info(
                    "Bound ACP child event to web_chat parent session %s "
                    "(external_id=%s source=%s)",
                    web_chat_parent.id,
                    external_id,
                    event.source.value,
                )
                return web_chat_parent.id
            self._logger.info(
                "Skipping auto-registration for ACP child process: external_id=%s source=%s",
                external_id,
                event.source.value,
            )
            return None

        self._logger.info(
            "Session not found via cache/DB lookup for external_id=%s "
            "(machine_id=%s, project_id=%s, source=%s); delegating to "
            "idempotent register_session (reuses existing row or creates).",
            external_id,
            machine_id,
            project_id,
            event.source.value,
        )
        cwd = hook_cwd(event.data, event.cwd)
        raw_terminal_context = event.data.get("terminal_context")
        terminal_context = raw_terminal_context if isinstance(raw_terminal_context, dict) else None
        terminal_context = enrich_terminal_context_with_cwd(terminal_context, cwd)
        platform_session_id = self._session_manager.register_session(
            external_id=external_id,
            machine_id=machine_id,
            project_id=project_id,
            transcript_path=event.data.get("transcript_path"),
            source=event.source.value,
            project_path=cwd,
            terminal_context=terminal_context,
        )
        event.metadata["_session_just_materialized"] = True
        return platform_session_id

    def _recover_compact_session(
        self,
        external_id: str,
        event: HookEvent,
        *,
        machine_id: str | None,
        project_id: str | None,
    ) -> tuple[bool, str | None]:
        """Recover compact identity, returning whether resolution is terminal."""
        compact_resolution = resolve_compact_continuation(
            self._session_manager.db,
            source=event.source.value,
            terminal_context=event.data.get("terminal_context"),
        )
        if compact_resolution.ambiguous:
            detail = {
                "error": "Compact continuation matches multiple persisted sessions.",
                "error_code": "compact_identity_ambiguous",
                "conflicting_session_ids": list(compact_resolution.conflicting_session_ids),
            }
            event.metadata["_session_resolution_error"] = detail
            self._logger.warning(
                "Skipping auto-registration for ambiguous compact continuation",
                extra={
                    "event": "compact_identity_ambiguous",
                    "observed_external_id": external_id,
                    **detail,
                },
            )
            return True, None

        if compact_resolution.session is None:
            return False, None

        canonical = compact_resolution.session
        activity = reconcile_compact_session_activity(
            self._session_manager,
            canonical.id,
        )
        if not activity.success:
            detail = activity.error_result()
            event.metadata["_session_resolution_error"] = detail
            self._logger.warning(
                "Skipping auto-registration for conflicting compact continuation",
                extra={
                    "event": "compact_identity_reactivation_blocked",
                    "session_id": canonical.id,
                    "observed_external_id": external_id,
                    **detail,
                },
            )
            return True, None

        event.metadata["_observed_external_id"] = external_id
        event.session_id = canonical.external_id
        self._session_manager.cache_session_mapping(
            external_id=canonical.external_id,
            source=event.source.value,
            session_id=canonical.id,
            project_id=canonical.project_id,
            session_type=canonical.session_type,
        )
        self._logger.info(
            "Recovered pre-start compact continuation as session %s",
            canonical.id,
            extra={
                "event": "compact_identity_prestart_recovered",
                "session_id": canonical.id,
                "canonical_external_id": canonical.external_id,
                "observed_external_id": external_id,
            },
        )
        return True, canonical.id

    def _enrich_task_context(self, platform_session_id: str, event: HookEvent) -> None:
        """Add active task context to event metadata."""
        explicit_task_id = event.task_id
        if explicit_task_id is not None:
            event.metadata["_task_id_origin"] = "explicit"
        try:
            # Get tasks linked with 'worked_on' action which implies active focus
            session_tasks = self._session_task_manager.get_session_tasks(platform_session_id)
            # Filter for active 'worked_on' tasks - taking the most recent one
            active_tasks = [t for t in session_tasks if t.get("action") == "worked_on"]
            task = None
            if explicit_task_id is not None:
                task = next(
                    (link["task"] for link in active_tasks if link["task"].id == explicit_task_id),
                    None,
                )
            elif active_tasks:
                task = active_tasks[0]["task"]
                event.task_id = task.id
                event.metadata["_task_id_origin"] = "session_context"

            if task is not None:
                event.metadata["_task_context"] = {
                    "id": task.id,
                    "title": task.title,
                    "state": _task_state_label(task),
                }
                # Keep legacy field for backwards compatibility
                event.metadata["_task_title"] = task.title
        except Exception as e:
            self._logger.warning("Failed to resolve active task: %s", e)
