"""
Event handlers module for hook event processing.

This module is extracted from hook_manager.py using Strangler Fig pattern.
It provides centralized event handler registration and dispatch.

Classes:
    EventHandlers: Manages event handler registration and dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.hooks.event_handlers._agent import AgentEventHandlerMixin
from gobby.hooks.event_handlers._misc import MiscEventHandlerMixin
from gobby.hooks.event_handlers._session import SessionEventHandlerMixin
from gobby.hooks.event_handlers._tool import EDIT_TOOLS, ToolEventHandlerMixin
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.session_types import HookSessionManager

if TYPE_CHECKING:
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.autonomous.progress_tracker import ProgressTracker
    from gobby.code_index.trigger import CodeIndexTrigger
    from gobby.config.tasks import WorkflowConfig
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.hooks.session_end_auto_link import SessionEndAutoLinkWorker
    from gobby.hooks.skill_manager import HookSkillManager
    from gobby.sessions.liveness_monitor import SessionLivenessMonitor
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.terminals import TerminalManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.workflows.hooks import WorkflowHookHandler


class EventHandlers(
    SessionEventHandlerMixin,
    AgentEventHandlerMixin,
    ToolEventHandlerMixin,
    MiscEventHandlerMixin,
):
    """
    Manages event handler registration and dispatch.

    Provides handler methods for all HookEventType values and a registration
    mechanism for looking up handlers by event type.

    Extracted from HookManager to separate event handling concerns.
    """

    def __init__(
        self,
        session_manager: HookSessionManager | None = None,
        workflow_handler: WorkflowHookHandler | None = None,
        session_storage: HookSessionManager | None = None,
        session_task_manager: SessionTaskManager | None = None,
        message_processor_resolver: Callable[[], Any | None] | None = None,
        task_manager: LocalTaskManager | None = None,
        progress_tracker: ProgressTracker | None = None,
        worktree_manager: LocalWorktreeManager | None = None,
        session_coordinator: SessionCoordinator | None = None,
        session_end_auto_link_worker: SessionEndAutoLinkWorker | None = None,
        skill_manager: HookSkillManager | None = None,
        call_tool: Callable[[str, str, dict[str, Any]], dict[str, Any] | None] | None = None,
        workflow_config: WorkflowConfig | None = None,
        workflow_config_resolver: Callable[[], WorkflowConfig | None] | None = None,
        get_machine_id: Callable[[], str | None] | None = None,
        resolve_project_id: Callable[[str | None, str | None], str] | None = None,
        code_index_trigger: CodeIndexTrigger | None = None,
        attention_metadata_store: AttentionMetadataStore | None = None,
        terminal_manager: TerminalManager | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize EventHandlers.

        Args:
            session_manager: SessionManager for session operations
            workflow_handler: WorkflowHookHandler for lifecycle workflows
            session_storage: Compatibility alias for session_manager
            session_task_manager: SessionTaskManager for session-task links
            message_processor_resolver: Resolves the current SessionMessageProcessor
            task_manager: LocalTaskManager for task operations
            session_coordinator: SessionCoordinator for session tracking
            session_end_auto_link_worker: Managed worker for session commit auto-linking
            skill_manager: HookSkillManager for skill discovery
            workflow_config: WorkflowConfig for workflow settings (debug_echo_context)
            workflow_config_resolver: Resolves current workflow settings
            get_machine_id: Function to get machine ID
            resolve_project_id: Function to resolve project ID from cwd
            code_index_trigger: Optional trigger for code indexing on file changes.
            event_loop: Daemon event loop used to schedule work from hook worker threads.
            logger: Optional logger instance
        """
        if (
            session_manager is not None
            and session_storage is not None
            and session_manager is not session_storage
        ):
            raise ValueError(
                "session_manager and session_storage must reference the same object "
                "when both are provided"
            )
        manager = session_manager if session_manager is not None else session_storage
        self._session_manager = manager
        self._liveness_monitor: SessionLivenessMonitor | None = None
        self._workflow_handler = workflow_handler
        self._session_task_manager = session_task_manager
        self._message_processor_resolver = message_processor_resolver or (lambda: None)
        self._session_message_processors: dict[str, Any] = {}
        self._task_manager = task_manager
        self._progress_tracker = progress_tracker
        self._worktree_manager = worktree_manager
        self._session_coordinator = session_coordinator
        self._session_end_auto_link_worker = session_end_auto_link_worker
        self._skill_manager = skill_manager
        self._call_tool = call_tool
        self._workflow_config_resolver = workflow_config_resolver or (lambda: workflow_config)
        self._get_machine_id = get_machine_id or (lambda: "unknown-machine")
        self._resolve_project_id = resolve_project_id or (lambda p, c: p or "")
        self._code_index_trigger = code_index_trigger
        self._attention_metadata_store = attention_metadata_store
        self.terminal_manager = terminal_manager
        self._event_loop = event_loop
        self._dispatch_session_summaries_fn: (
            Callable[[str, bool, threading.Event | None, bool], None] | None
        ) = None
        self.logger = logger or logging.getLogger(__name__)

        # Build handler map
        self._handler_map: dict[HookEventType, Callable[[HookEvent], HookResponse]] = {
            HookEventType.SESSION_START: self.handle_session_start,
            HookEventType.SESSION_END: self.handle_session_end,
            HookEventType.SETUP: self.handle_neutral,
            HookEventType.BEFORE_AGENT: self.handle_before_agent,
            HookEventType.USER_PROMPT_EXPANSION: self.handle_neutral,
            HookEventType.AFTER_AGENT: self.handle_after_agent,
            HookEventType.BEFORE_TOOL: self.handle_before_tool,
            HookEventType.AFTER_TOOL: self.handle_after_tool,
            HookEventType.POST_TOOL_BATCH: self.handle_neutral,
            HookEventType.PRE_COMPACT: self.handle_pre_compact,
            HookEventType.POST_COMPACT: self.handle_post_compact,
            HookEventType.SUBAGENT_START: self.handle_subagent_start,
            HookEventType.SUBAGENT_STOP: self.handle_subagent_stop,
            HookEventType.NOTIFICATION: self.handle_notification,
            HookEventType.MESSAGE_DISPLAY: self.handle_neutral,
            HookEventType.DIRECTORY_ADDED: self.handle_neutral,
            HookEventType.BEFORE_TOOL_SELECTION: self.handle_before_tool_selection,
            HookEventType.BEFORE_MODEL: self.handle_before_model,
            HookEventType.AFTER_MODEL: self.handle_after_model,
            HookEventType.PERMISSION_REQUEST: self.handle_permission_request,
            HookEventType.PERMISSION_DENIED: self.handle_permission_denied,
            HookEventType.STOP: self.handle_stop,
            HookEventType.STOP_FAILURE: self.handle_stop_failure,
            HookEventType.TASK_CREATED: self.handle_task_created,
            HookEventType.TASK_COMPLETED: self.handle_task_completed,
            HookEventType.TEAMMATE_IDLE: self.handle_teammate_idle,
            HookEventType.INSTRUCTIONS_LOADED: self.handle_instructions_loaded,
            HookEventType.CONFIG_CHANGE: self.handle_config_change,
            HookEventType.CWD_CHANGED: self.handle_cwd_changed,
            HookEventType.FILE_CHANGED: self.handle_file_changed,
            HookEventType.WORKTREE_CREATE: self.handle_worktree_create,
            HookEventType.WORKTREE_REMOVE: self.handle_worktree_remove,
            HookEventType.ELICITATION: self.handle_elicitation,
            HookEventType.ELICITATION_RESULT: self.handle_elicitation_result,
        }

    def set_liveness_monitor(self, monitor: SessionLivenessMonitor | None) -> None:
        """Connect the daemon's session liveness monitor to lifecycle hooks."""
        self._liveness_monitor = monitor

    def set_attention_metadata_store(self, store: AttentionMetadataStore | None) -> None:
        """Connect transient agent metadata to lifecycle hooks."""
        self._attention_metadata_store = store

    def get_handler(
        self, event_type: HookEventType | str
    ) -> Callable[[HookEvent], HookResponse] | None:
        """
        Get handler for an event type.

        Args:
            event_type: The event type to get handler for

        Returns:
            Handler callable or None if not found
        """
        if isinstance(event_type, str):
            try:
                event_type = HookEventType(event_type)
            except ValueError:
                return None
        return self._handler_map.get(event_type)

    def get_handler_map(self) -> dict[HookEventType, Callable[[HookEvent], HookResponse]]:
        """
        Get a copy of the handler map.

        Returns:
            Copy of handler map (modifications don't affect internal state)
        """
        return dict(self._handler_map)
