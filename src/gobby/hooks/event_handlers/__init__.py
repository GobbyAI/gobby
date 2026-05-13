"""
Event handlers module for hook event processing.

This module is extracted from hook_manager.py using Strangler Fig pattern.
It provides centralized event handler registration and dispatch.

Classes:
    EventHandlers: Manages event handler registration and dispatch.
"""

from __future__ import annotations

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
    from gobby.code_index.trigger import CodeIndexTrigger
    from gobby.config.skills import SkillsConfig
    from gobby.config.tasks import WorkflowConfig
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.hooks.skill_manager import HookSkillManager
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.tasks import LocalTaskManager
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
        message_processor: Any | None = None,
        task_manager: LocalTaskManager | None = None,
        worktree_manager: LocalWorktreeManager | None = None,
        session_coordinator: SessionCoordinator | None = None,
        skill_manager: HookSkillManager | None = None,
        skills_config: SkillsConfig | None = None,
        call_tool: Callable[[str, str, dict[str, Any]], dict[str, Any] | None] | None = None,
        workflow_config: WorkflowConfig | None = None,
        get_machine_id: Callable[[], str] | None = None,
        resolve_project_id: Callable[[str | None, str | None], str] | None = None,
        code_index_trigger: CodeIndexTrigger | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize EventHandlers.

        Args:
            session_manager: SessionManager for session operations
            workflow_handler: WorkflowHookHandler for lifecycle workflows
            session_storage: Compatibility alias for session_manager
            session_task_manager: SessionTaskManager for session-task links
            message_processor: SessionMessageProcessor for message handling
            task_manager: LocalTaskManager for task operations
            session_coordinator: SessionCoordinator for session tracking
            skill_manager: HookSkillManager for skill discovery
            skills_config: SkillsConfig for skill discovery/manifest settings
            workflow_config: WorkflowConfig for workflow settings (debug_echo_context)
            get_machine_id: Function to get machine ID
            resolve_project_id: Function to resolve project ID from cwd
            code_index_trigger: Optional trigger for code indexing on file changes.
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
        self._workflow_handler = workflow_handler
        self._session_task_manager = session_task_manager
        self._message_processor = message_processor
        self._task_manager = task_manager
        self._worktree_manager = worktree_manager
        self._session_coordinator = session_coordinator
        self._skill_manager = skill_manager
        self._skills_config = skills_config
        self._call_tool = call_tool
        self._workflow_config = workflow_config
        self._get_machine_id = get_machine_id or (lambda: "unknown-machine")
        self._resolve_project_id = resolve_project_id or (lambda p, c: p or "")
        self._code_index_trigger = code_index_trigger
        self._pending_subagent_depths: dict[str, int] = {}
        self._dispatch_session_summaries_fn: (
            Callable[[str, bool, threading.Event | None, bool], None] | None
        ) = None
        self.logger = logger or logging.getLogger(__name__)

        # Build handler map
        self._handler_map: dict[HookEventType, Callable[[HookEvent], HookResponse]] = {
            HookEventType.SESSION_START: self.handle_session_start,
            HookEventType.SESSION_END: self.handle_session_end,
            HookEventType.BEFORE_AGENT: self.handle_before_agent,
            HookEventType.AFTER_AGENT: self.handle_after_agent,
            HookEventType.BEFORE_TOOL: self.handle_before_tool,
            HookEventType.AFTER_TOOL: self.handle_after_tool,
            HookEventType.PRE_COMPACT: self.handle_pre_compact,
            HookEventType.POST_COMPACT: self.handle_post_compact,
            HookEventType.SUBAGENT_START: self.handle_subagent_start,
            HookEventType.SUBAGENT_STOP: self.handle_subagent_stop,
            HookEventType.NOTIFICATION: self.handle_notification,
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
