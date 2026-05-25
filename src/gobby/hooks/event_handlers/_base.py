from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.session_types import HookSessionManager

if TYPE_CHECKING:
    from gobby.config.sessions import MemoryRecallHelperConfig
    from gobby.config.skills import SkillsConfig
    from gobby.config.tasks import WorkflowConfig
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.hooks.skill_manager import HookSkillManager
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.workflows.hooks import WorkflowHookHandler

# The final bool is set_handoff_ready for handoff-gated summary dispatches.
DispatchSessionSummariesFn = Callable[[str, bool, threading.Event | None, bool], None]


class EventHandlersBase:
    """Base class for EventHandlers mixins with type hints for shared state."""

    _session_manager: HookSessionManager | None
    _workflow_handler: WorkflowHookHandler | None
    _workflow_config: WorkflowConfig | None
    _session_task_manager: SessionTaskManager | None
    _message_processor: Any | None
    _task_manager: LocalTaskManager | None
    _worktree_manager: LocalWorktreeManager | None
    _session_coordinator: SessionCoordinator | None
    _skill_manager: HookSkillManager | None
    _skills_config: SkillsConfig | None
    _memory_recall_helper_config: MemoryRecallHelperConfig | None
    _call_tool: Callable[[str, str, dict[str, Any]], dict[str, Any] | None] | None
    _get_machine_id: Callable[[], str]
    _resolve_project_id: Callable[[str | None, str | None], str]
    _code_index_trigger: Any | None
    _pending_subagent_depths: dict[str, int]
    _dispatch_session_summaries_fn: DispatchSessionSummariesFn | None
    logger: logging.Logger
    _handler_map: dict[HookEventType, Callable[[HookEvent], HookResponse]]

    def _apply_debug_echo(self, response: HookResponse) -> None:
        """Append additionalContext to system_message when debug_echo_context is enabled.

        Reads the flag from WorkflowConfig.
        Mutates ``response`` in place (HookResponse is a non-frozen dataclass).
        """
        debug_echo = bool(self._workflow_config and self._workflow_config.debug_echo_context)

        if not debug_echo or not response.context:
            return

        echo_block = f"\n\n[DEBUG additionalContext]\n{response.context}"
        if response.system_message:
            response.system_message += echo_block
        else:
            response.system_message = echo_block
