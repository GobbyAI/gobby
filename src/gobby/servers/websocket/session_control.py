"""WebSocket session control handlers.

SessionControlMixin provides session lifecycle operations: stop, clear,
delete, mode changes, project switching, plan approval, continue-in-chat,
and idle session cleanup. Each handler group lives in a dedicated module
under ``handlers/``; this mixin is a thin router that dispatches to them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.handlers import plan_approval as _plan
from gobby.servers.websocket.handlers import session_config as _config
from gobby.servers.websocket.handlers import session_lifecycle as _lifecycle
from gobby.servers.websocket.handlers import session_observe as _observe

logger = logging.getLogger(__name__)


class SessionControlMixin:
    """Mixin providing session control handlers for WebSocketServer.

    Requires on the host class:
    - ``self.clients: dict[Any, dict[str, Any]]``
    - ``self._chat_sessions: dict[str, ChatSession]``
    - ``self._active_chat_tasks: dict[str, asyncio.Task[None]]``
    - ``self._pending_modes: dict[str, str]``
    - ``self._cancel_active_chat(...)`` (from ChatMixin)
    - ``self._send_error(...)`` (from HandlerMixin)
    - ``self._create_chat_session(...)`` (from ChatMixin)
    """

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSessionProtocol]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]
    _pending_projects: dict[str, str]
    _pending_providers: dict[str, str]

    # Provided by ChatMixin / HandlerMixin – declared for type checking only.
    if TYPE_CHECKING:

        async def _cancel_active_chat(self, conversation_id: str) -> None: ...

        async def _fire_session_end(self, conversation_id: str) -> None: ...

        async def _send_error(
            self,
            websocket: Any,
            message: str,
            request_id: str | None = None,
            code: str = "ERROR",
        ) -> None: ...

        async def _create_chat_session(
            self,
            conversation_id: str,
            model: str | None = None,
            project_id: str | None = None,
            resume_session_id: str | None = None,
            provider: str | None = None,
            reasoning_effort: str | None = None,
        ) -> ChatSessionProtocol: ...

    # -- Session lifecycle handlers ------------------------------------------

    async def _handle_stop_chat(self, websocket: Any, data: dict[str, Any] | None = None) -> None:
        await _lifecycle.handle_stop_chat(self, websocket, data)

    async def _handle_clear_chat(self, websocket: Any, data: dict[str, Any]) -> None:
        await _lifecycle.handle_clear_chat(self, websocket, data)

    async def _handle_delete_chat(self, websocket: Any, data: dict[str, Any]) -> None:
        await _lifecycle.handle_delete_chat(self, websocket, data)

    async def _cleanup_idle_sessions(self) -> None:
        await _lifecycle.cleanup_idle_sessions(self)

    # -- Plan approval handlers ----------------------------------------------

    async def _handle_plan_approval_response(self, websocket: Any, data: dict[str, Any]) -> None:
        await _plan.handle_plan_approval_response(self, websocket, data)

    async def _handle_recovered_plan_approval(
        self, websocket: Any, conversation_id: str, data: dict[str, Any]
    ) -> None:
        await _plan.handle_recovered_plan_approval(self, websocket, conversation_id, data)

    async def _rebroadcast_pending_interactions(
        self, websocket: Any, conversation_ids: list[str]
    ) -> None:
        """Rebroadcast pending interactions for active conversations on reconnect."""
        import json as _json

        manager = getattr(self, "_pending_interaction_manager", None)
        if not manager:
            return

        for conv_id in conversation_ids:
            session = self._chat_sessions.get(conv_id)
            if not session or not session.db_session_id:
                continue
            try:
                pending = await manager.rebroadcast(session.db_session_id)
                for interaction in pending:
                    interaction_id = interaction.get("interaction_id")
                    if not isinstance(interaction_id, str) or not interaction_id:
                        logger.warning(
                            "Skipping pending interaction missing interaction_id for %s: %r",
                            conv_id,
                            interaction,
                        )
                        continue
                    msg = _json.dumps(
                        {
                            "type": "tool_status"
                            if interaction.get("kind") == "tool"
                            else "pending_interaction",
                            "conversation_id": conv_id,
                            "interaction": interaction,
                            "message_id": f"pending-interaction-{interaction_id}",
                            "tool_call_id": interaction_id,
                            "status": "pending_approval",
                            "tool_name": interaction.get("tool_name"),
                            "arguments": interaction.get("arguments", {}),
                        }
                    )
                    await websocket.send(msg)
            except Exception:
                logger.debug(f"Failed to rebroadcast interactions for {conv_id}", exc_info=True)

    # -- Session configuration handlers --------------------------------------

    async def _handle_set_mode(self, websocket: Any, data: dict[str, Any]) -> None:
        await _config.handle_set_mode(self, websocket, data)

    async def _handle_set_project(self, websocket: Any, data: dict[str, Any]) -> None:
        await _config.handle_set_project(self, websocket, data)

    async def _handle_set_worktree(self, websocket: Any, data: dict[str, Any]) -> None:
        await _config.handle_set_worktree(self, websocket, data)

    async def _handle_set_agent(self, websocket: Any, data: dict[str, Any]) -> None:
        await _config.handle_set_agent(self, websocket, data)

    async def _handle_set_provider(self, websocket: Any, data: dict[str, Any]) -> None:
        await _config.handle_set_provider(self, websocket, data)

    # -- Session observation handlers ----------------------------------------

    async def _handle_continue_in_chat(self, websocket: Any, data: dict[str, Any]) -> None:
        await _observe.handle_continue_in_chat(self, websocket, data)

    async def _check_resume_blocked(self, source_session: Any) -> str | None:
        return await _observe.check_resume_blocked(self, source_session)

    async def _handle_attach_to_session(self, websocket: Any, data: dict[str, Any]) -> None:
        await _observe.handle_attach_to_session(self, websocket, data)

    async def _handle_send_to_cli_session(self, websocket: Any, data: dict[str, Any]) -> None:
        await _observe.handle_send_to_cli_session(self, websocket, data)

    async def _handle_detach_from_session(self, websocket: Any, data: dict[str, Any]) -> None:
        await _observe.handle_detach_from_session(self, websocket, data)
