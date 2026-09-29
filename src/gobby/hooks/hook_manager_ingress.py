"""Per-event ingress bookkeeping for HookManager: machine, transcript, activity, refs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg

from gobby.hooks.events import HookEvent, HookEventType
from gobby.hooks.session_ref_resolution import resolve_session_refs_in_tool_input
from gobby.hooks.session_types import HookSessionManager
from gobby.sessions.activity import record_session_activity
from gobby.storage.machines import LocalMachineManager
from gobby.utils.session_refs import try_resolve_session_field

if TYPE_CHECKING:
    from gobby.hooks.event_handlers import EventHandlers


def _hook_text_field(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class HookManagerIngressMixin:
    """Bookkeeping each hook event performs on arrival, owned by HookManager."""

    _database: Any
    _session_manager: HookSessionManager
    _event_handlers: EventHandlers
    _pending_transcript_rechecks: dict[str, int]
    logger: logging.Logger
    get_machine_id: Callable[[], str | None]

    def _record_machine_ingress(self, event: HookEvent) -> None:
        db = self._database or getattr(self._session_manager, "db", None)
        if db is None:
            return

        data = event.data if isinstance(event.data, dict) else {}
        machine_id = None
        for candidate in (
            event.machine_id,
            _hook_text_field(data, "machine_id", "machineId"),
        ):
            try:
                machine_id = str(UUID(candidate.strip())) if candidate else None
            except (AttributeError, ValueError):
                # Hook payloads are untrusted input; a non-UUID identity is
                # unattributable, never fatal to hook processing.
                self.logger.debug(
                    "Ignoring non-UUID machine id from hook ingress",
                    extra={"machine_id": candidate},
                )
                continue
            if machine_id is not None:
                break
        if machine_id is None:
            return
        try:
            machine = LocalMachineManager(db).refresh_seen(
                machine_id,
                hostname=_hook_text_field(data, "hostname", "host_name", "host"),
                os=_hook_text_field(data, "os", "platform", "operating_system"),
                label=_hook_text_field(data, "machine_label", "machineLabel"),
                tailscale_name=_hook_text_field(data, "tailscale_name", "tailscaleName"),
            )
            if machine is None:
                self.logger.debug(
                    "Ignoring unknown machine id from hook ingress",
                    extra={"machine_id": machine_id},
                )
        except psycopg.Error as exc:
            self.logger.debug(
                "Failed to refresh machine registry from hook ingress",
                extra={"error": str(exc), "machine_id": machine_id},
                exc_info=True,
            )

    def _recheck_pending_transcript(self, event: HookEvent) -> None:
        """Complete a pending transcript association after canonical session resolve."""
        if event.event_type == HookEventType.SESSION_END:
            self._discard_pending_transcript_recheck(event)
            return
        if event.event_type not in {
            HookEventType.BEFORE_TOOL,
            HookEventType.AFTER_TOOL,
            HookEventType.AFTER_AGENT,
            HookEventType.STOP,
        }:
            return
        from gobby.hooks.event_handlers._session_start.transcripts import (
            recheck_pending_transcript_path,
            replace_session_message_processor,
        )

        tracking = recheck_pending_transcript_path(
            event,
            session_manager=self._session_manager,
            budgets=self._pending_transcript_rechecks,
            local_machine_id=self.get_machine_id(),
        )
        if tracking is not None:
            session_id, transcript_path, source = tracking
            handler = self._event_handlers
            processor = handler._resolve_message_processor()
            if processor is not None:
                # Registration is idempotent for an unchanged path; it also repairs
                # pre-created sessions whose initial SessionStart never arrived.
                replace_session_message_processor(
                    handler, session_id, processor, transcript_path, source=source
                )

    def _discard_pending_transcript_recheck(self, event: HookEvent) -> None:
        """Drop the resolved session's bounded recheck budget.

        A session start opens a fresh recheck window for that platform session
        (a resumed or revived row must not inherit an exhausted budget), and a
        session end closes the hook stream that could still complete the
        association, so the entry would otherwise outlive the session.
        """
        platform_session_id = event.metadata.get("_platform_session_id")
        if isinstance(platform_session_id, str) and platform_session_id:
            self._pending_transcript_rechecks.pop(platform_session_id, None)

    @staticmethod
    def _record_session_activity_pulse(event: HookEvent) -> None:
        """Record a non-statusline activity pulse for the event's platform session."""
        platform_id = event.metadata.get("_platform_session_id")
        if isinstance(platform_id, str) and platform_id:
            record_session_activity(platform_id)

    def _resolve_session_refs_in_tool_input(self, event: HookEvent) -> None:
        """Resolve #N session references to UUIDs in MCP tool arguments."""
        resolve_session_refs_in_tool_input(event, self._session_manager)

    def _try_resolve_session_field(
        self, d: dict[str, Any], field: str, project_id: str | None
    ) -> bool:
        """Resolve a #N session reference in d[field] to UUID in place."""
        return try_resolve_session_field(
            d,
            field,
            session_manager=self._session_manager,
            project_id=project_id,
        )
