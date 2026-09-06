from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.app_context import get_app_context
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.session_types import HookSessionManager
from gobby.sessions.turn_lifecycle import (
    TurnDisposition,
    TurnEvidence,
    TurnLifecycleReducer,
    WaitKind,
    WaitResolution,
)

if TYPE_CHECKING:
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.autonomous.progress_tracker import ProgressTracker
    from gobby.config.tasks import WorkflowConfig
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.hooks.session_end_auto_link import SessionEndAutoLinkWorker
    from gobby.hooks.skill_manager import HookSkillManager
    from gobby.sessions.liveness_monitor import SessionLivenessMonitor
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.workflows.hooks import WorkflowHookHandler

# The final bool is set_awaiting_handoff for handoff-gated summary dispatches.
DispatchSessionSummariesFn = Callable[[str, bool, threading.Event | None, bool], None]


class EventHandlersBase:
    """Base class for EventHandlers mixins with type hints for shared state."""

    _session_manager: HookSessionManager | None
    _liveness_monitor: SessionLivenessMonitor | None
    _attention_metadata_store: AttentionMetadataStore | None
    _workflow_handler: WorkflowHookHandler | None
    _workflow_config_resolver: Callable[[], WorkflowConfig | None]
    _session_task_manager: SessionTaskManager | None
    _message_processor_resolver: Callable[[], Any | None]
    _session_message_processors: dict[str, Any]
    _task_manager: LocalTaskManager | None
    _progress_tracker: ProgressTracker | None
    _worktree_manager: LocalWorktreeManager | None
    _session_coordinator: SessionCoordinator | None
    _session_end_auto_link_worker: SessionEndAutoLinkWorker | None
    _skill_manager: HookSkillManager | None
    _call_tool: Callable[[str, str, dict[str, Any]], dict[str, Any] | None] | None
    _get_machine_id: Callable[[], str | None]
    _resolve_project_id: Callable[[str | None, str | None], str]
    _code_index_trigger: Any | None
    _dispatch_session_summaries_fn: DispatchSessionSummariesFn | None
    _event_loop: asyncio.AbstractEventLoop | None
    _agent_run_manager: LocalAgentRunManager | None
    _terminal_runtime_registry: Any | None
    _turn_lifecycle: TurnLifecycleReducer | None
    logger: logging.Logger
    _handler_map: dict[HookEventType, Callable[[HookEvent], HookResponse]]

    def get_session_manager(self) -> HookSessionManager | None:
        """Return the configured hook session manager, if available."""
        return self._session_manager

    @staticmethod
    def _turn_evidence(event: HookEvent) -> TurnEvidence:
        return TurnEvidence(
            source=event.source.value,
            provider_turn_key=event.provider_turn_key,
            request_id=event.request_id,
            cursor=event.protocol_cursor,
        )

    def _begin_turn_lifecycle(self, event: HookEvent) -> None:
        session_id = event.metadata.get("_platform_session_id")
        if not isinstance(session_id, str) or not session_id or self._turn_lifecycle is None:
            return
        try:
            self._turn_lifecycle.begin_turn(session_id, self._turn_evidence(event))
        except Exception:
            self.logger.warning("Failed to begin turn lifecycle", exc_info=True)

    def _resume_turn_lifecycle(self, event: HookEvent) -> None:
        session_id = event.metadata.get("_platform_session_id")
        if not isinstance(session_id, str) or not session_id or self._turn_lifecycle is None:
            return
        try:
            self._turn_lifecycle.resumed_work(session_id, self._turn_evidence(event))
        except Exception:
            self.logger.warning("Failed to resume turn lifecycle", exc_info=True)

    def _end_turn_lifecycle(self, event: HookEvent, disposition: TurnDisposition) -> None:
        session_id = event.metadata.get("_platform_session_id")
        if not isinstance(session_id, str) or not session_id or self._turn_lifecycle is None:
            return
        try:
            self._turn_lifecycle.end_turn(
                session_id,
                disposition,
                self._turn_evidence(event),
            )
        except Exception:
            self.logger.warning("Failed to end turn lifecycle", exc_info=True)

    def _enter_turn_wait(self, event: HookEvent, kind: WaitKind) -> None:
        session_id = event.metadata.get("_platform_session_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or self._turn_lifecycle is None
            or not event.wait_token
        ):
            return
        try:
            self._turn_lifecycle.enter_wait(
                session_id,
                kind=kind,
                token=event.wait_token,
                evidence=self._turn_evidence(event),
            )
        except Exception:
            self.logger.warning("Failed to enter turn wait", exc_info=True)

    def _resolve_turn_wait(
        self,
        event: HookEvent,
        resolution: WaitResolution,
    ) -> None:
        session_id = event.metadata.get("_platform_session_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or self._turn_lifecycle is None
            or not event.wait_token
        ):
            return
        try:
            self._turn_lifecycle.resolve_wait(
                session_id,
                token=event.wait_token,
                resolution=resolution,
                evidence=self._turn_evidence(event),
            )
        except Exception:
            self.logger.warning("Failed to resolve turn wait", exc_info=True)

    def _resolve_message_processor(self) -> Any | None:
        return self._message_processor_resolver()

    def _shutdown_in_progress(self) -> bool:
        """Return whether the daemon app context is tearing down."""
        app_context = get_app_context()
        return bool(getattr(app_context, "shutdown_in_progress", False))

    def _skip_session_status_update_during_shutdown(
        self,
        event_name: str,
        session_id: str,
        status: str,
    ) -> bool:
        if not self._shutdown_in_progress():
            return False
        self.logger.debug(
            "%s: skipping session %s status update to %s during daemon shutdown",
            event_name,
            session_id,
            status,
        )
        return True

    def _apply_debug_echo(self, response: HookResponse) -> None:
        """Append additionalContext to system_message when debug_echo_context is enabled.

        Reads the flag from WorkflowConfig.
        Mutates ``response`` in place (HookResponse is a non-frozen dataclass).
        """
        workflow_config = self._workflow_config_resolver()
        debug_echo = bool(workflow_config and workflow_config.debug_echo_context)

        if not debug_echo or not response.context:
            return

        echo_block = f"\n\n[DEBUG additionalContext]\n{response.context}"
        if response.system_message:
            response.system_message += echo_block
        else:
            response.system_message = echo_block
