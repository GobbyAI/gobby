from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse

if TYPE_CHECKING:
    from gobby.config.skills import SkillsConfig
    from gobby.config.tasks import WorkflowConfig
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.hooks.skill_manager import HookSkillManager
    from gobby.sessions.manager import SessionManager
    from gobby.storage.session_messages import LocalSessionMessageManager
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.sessions import LocalSessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.workflows.hooks import WorkflowHookHandler


class EventHandlersBase:
    """Base class for EventHandlers mixins with type hints for shared state."""

    _session_manager: SessionManager | None
    _workflow_handler: WorkflowHookHandler | None
    _workflow_config: WorkflowConfig | None
    _session_storage: LocalSessionManager | None
    _session_task_manager: SessionTaskManager | None
    _message_processor: Any | None
    _task_manager: LocalTaskManager | None
    _session_coordinator: SessionCoordinator | None
    _message_manager: LocalSessionMessageManager | None
    _skill_manager: HookSkillManager | None
    _skills_config: SkillsConfig | None
    _get_machine_id: Callable[[], str]
    _resolve_project_id: Callable[[str | None, str | None], str]
    _dispatch_boundary_summaries_fn: Callable[[str, bool], None] | None
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
