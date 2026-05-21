"""Daemon-owned registry for live web-chat sessions."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from gobby.llm.claude_models import DoneEvent
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.sessions.compact_continuation import COMPACT_SELF_CONTINUE_PROMPT

logger = logging.getLogger(__name__)

WEB_CHAT_WAKE_PROMPT = "Message from Gobby daemon: Task completed."


class WebChatSessionRegistry:
    """Shared live-session registry used by WebSocket chat and MCP tools."""

    def __init__(self) -> None:
        self.sessions: dict[str, ChatSessionProtocol] = {}
        self.active_tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_compactions: dict[str, str] = {}
        self._queued_compaction_tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_wakes: dict[str, tuple[str, str]] = {}
        self._queued_wake_tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, conversation_id: str, session: ChatSessionProtocol) -> None:
        """Register a live chat session by conversation id."""
        self.sessions[conversation_id] = session

    def unregister(self, conversation_id: str) -> None:
        """Remove a session and any queued registry state."""
        self.sessions.pop(conversation_id, None)
        self.active_tasks.pop(conversation_id, None)
        self._queued_compactions.pop(conversation_id, None)
        self._queued_wakes.pop(conversation_id, None)
        queued_task = self._queued_compaction_tasks.pop(conversation_id, None)
        if queued_task is not None and not queued_task.done():
            queued_task.cancel()
        queued_wake_task = self._queued_wake_tasks.pop(conversation_id, None)
        if queued_wake_task is not None and not queued_wake_task.done():
            queued_wake_task.cancel()

    def clear(self) -> None:
        """Clear all live registry state."""
        for task in self._queued_compaction_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._queued_wake_tasks.values():
            if not task.done():
                task.cancel()
        self.sessions.clear()
        self.active_tasks.clear()
        self._queued_compactions.clear()
        self._queued_compaction_tasks.clear()
        self._queued_wakes.clear()
        self._queued_wake_tasks.clear()

    def find_session(self, session_id: str) -> tuple[str | None, ChatSessionProtocol | None]:
        """Find a live session by conversation id or DB session id."""
        session = self.sessions.get(session_id)
        if session is not None:
            return session_id, session

        for conversation_id, candidate in list(self.sessions.items()):
            if getattr(candidate, "db_session_id", None) == session_id:
                return conversation_id, candidate
            if getattr(candidate, "conversation_id", None) == session_id:
                return conversation_id, candidate
        return None, None

    def track_active_task(self, conversation_id: str, task: asyncio.Task[None]) -> None:
        """Track an active streaming turn for a conversation."""
        self.active_tasks[conversation_id] = task
        task.add_done_callback(
            lambda done_task: self._on_active_task_done(conversation_id, done_task)
        )

    def clear_active_task(
        self,
        conversation_id: str,
        task: asyncio.Task[None] | None = None,
    ) -> None:
        """Clear active task state when the tracked task completes."""
        active_task = self.active_tasks.get(conversation_id)
        if task is None or active_task is task:
            self.active_tasks.pop(conversation_id, None)

    def has_active_turn(self, conversation_id: str) -> bool:
        """Return True when a conversation has a live streaming task."""
        active_task = self.active_tasks.get(conversation_id)
        if active_task is None:
            return False
        if active_task.done():
            self.active_tasks.pop(conversation_id, None)
            return False
        return True

    async def compact_session(
        self,
        session_id: str,
        command: str = "/compact",
    ) -> dict[str, Any]:
        """Trigger a web-chat compaction command on a live session."""
        conversation_id, session = self.find_session(session_id)
        if conversation_id is None or session is None:
            return {
                "compacted": False,
                "reason": f"No live web_chat session found for {session_id}",
            }

        if self.has_active_turn(conversation_id):
            self._queued_compactions[conversation_id] = command
            return {
                "compacted": True,
                "command": command,
                "via": "web_chat",
                "queued": True,
            }

        result = await self._drain_compaction(
            session,
            command,
            continuation_prompt=COMPACT_SELF_CONTINUE_PROMPT,
        )
        if not result.get("compacted"):
            return result
        return {
            "compacted": True,
            "command": command,
            "via": "web_chat",
            "queued": False,
        }

    async def wake_session(
        self,
        session_id: str,
        message: str = WEB_CHAT_WAKE_PROMPT,
    ) -> dict[str, Any]:
        """Trigger a hidden web-chat turn so pending mailbox context is injected."""
        conversation_id, session = self.find_session(session_id)
        if conversation_id is None or session is None:
            return self._no_live_web_chat_result(session_id)

        if self.has_active_turn(conversation_id) or self._has_running_queued_task(conversation_id):
            self._queued_wakes[conversation_id] = (session_id, message)
            return {
                "session_id": session_id,
                "delivered": True,
                "method": "web_chat",
                "queued": True,
            }

        return await self._drain_wake_session(
            requested_session_id=session_id,
            conversation_id=conversation_id,
            session=session,
            message=message,
            queued=False,
        )

    def _on_active_task_done(
        self,
        conversation_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self.clear_active_task(conversation_id, task)
        command = self._queued_compactions.pop(conversation_id, None)
        wake_request = self._queued_wakes.pop(conversation_id, None)
        if command is None and wake_request is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Cannot run queued web_chat work without an event loop")
            return

        queued_task = loop.create_task(
            self._run_queued_after_turn(conversation_id, command, wake_request)
        )
        if command is not None:
            self._queued_compaction_tasks[conversation_id] = queued_task
            queued_task.add_done_callback(
                lambda done_task: self._on_queued_compaction_done(
                    conversation_id,
                    done_task,
                )
            )
        if wake_request is not None:
            self._queued_wake_tasks[conversation_id] = queued_task
            queued_task.add_done_callback(
                lambda done_task: self._on_queued_wake_done(conversation_id, done_task)
            )

    def _on_queued_compaction_done(
        self,
        conversation_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._queued_compaction_tasks.pop(conversation_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Queued web_chat compaction failed for %s",
                conversation_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        self._schedule_queued_wake_if_idle(conversation_id)

    async def _run_queued_compaction(self, conversation_id: str, command: str) -> None:
        await self._run_queued_after_turn(conversation_id, command, None)

    async def _run_queued_after_turn(
        self,
        conversation_id: str,
        command: str | None,
        wake_request: tuple[str, str] | None,
    ) -> None:
        if self.has_active_turn(conversation_id):
            if command is not None:
                self._queued_compactions[conversation_id] = command
            if wake_request is not None:
                self._queued_wakes[conversation_id] = wake_request
            return

        if command is not None:
            result = await self.compact_session(conversation_id, command=command)
            if not result.get("compacted"):
                logger.warning(
                    "Queued web_chat compaction failed for %s: %s",
                    conversation_id,
                    result.get("reason", "unknown error"),
                )

        wake_request = self._queued_wakes.pop(conversation_id, wake_request)
        while wake_request is not None:
            requested_session_id, message = wake_request
            _, session = self.find_session(conversation_id)
            if session is None:
                logger.warning(
                    "Queued web_chat wake failed for %s: no live session",
                    conversation_id,
                )
                return
            result = await self._drain_wake_session(
                requested_session_id=requested_session_id,
                conversation_id=conversation_id,
                session=session,
                message=message,
                queued=True,
            )
            if not result.get("delivered"):
                logger.warning(
                    "Queued web_chat wake failed for %s: %s",
                    conversation_id,
                    result.get("error_message") or result.get("error") or "unknown error",
                )
            wake_request = self._queued_wakes.pop(conversation_id, None)

    def _on_queued_wake_done(
        self,
        conversation_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._queued_wake_tasks.pop(conversation_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Queued web_chat wake failed for %s",
                conversation_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        self._schedule_queued_wake_if_idle(conversation_id)

    def _schedule_queued_wake_if_idle(self, conversation_id: str) -> None:
        if self.has_active_turn(conversation_id):
            return
        if self._has_running_queued_task(conversation_id):
            return
        wake_request = self._queued_wakes.pop(conversation_id, None)
        if wake_request is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._queued_wakes[conversation_id] = wake_request
            logger.warning("Cannot run queued web_chat wake without an event loop")
            return
        queued_task = loop.create_task(
            self._run_queued_after_turn(conversation_id, None, wake_request)
        )
        self._queued_wake_tasks[conversation_id] = queued_task
        queued_task.add_done_callback(
            lambda done_task: self._on_queued_wake_done(conversation_id, done_task)
        )

    def _has_running_queued_task(self, conversation_id: str) -> bool:
        tasks = (
            self._queued_compaction_tasks.get(conversation_id),
            self._queued_wake_tasks.get(conversation_id),
        )
        return any(task is not None and not task.done() for task in tasks)

    async def _drain_wake_session(
        self,
        *,
        requested_session_id: str,
        conversation_id: str,
        session: ChatSessionProtocol,
        message: str,
        queued: bool,
    ) -> dict[str, Any]:
        result = await self._drain_message_until_done(
            session,
            message,
            action="web_chat wake",
        )
        if not result.get("ok"):
            return {
                "session_id": requested_session_id,
                "delivered": False,
                "method": "web_chat",
                "queued": queued,
                "error": result["reason"],
                "error_code": "web_chat_wake_failed",
                "error_message": result["reason"],
            }
        return {
            "session_id": requested_session_id,
            "conversation_id": conversation_id,
            "delivered": True,
            "method": "web_chat",
            "queued": queued,
        }

    @staticmethod
    def _no_live_web_chat_result(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "delivered": False,
            "method": "web_chat",
            "error": "no_live_web_chat_session",
            "error_code": "no_live_web_chat_session",
            "error_message": f"No live web_chat session found for {session_id}",
        }

    async def _drain_compaction(
        self,
        session: ChatSessionProtocol,
        command: str,
        *,
        continuation_prompt: str | None = None,
    ) -> dict[str, Any]:
        compact_result = await self._drain_message_until_done(
            session,
            command,
            action="web_chat compaction",
        )
        if not compact_result.get("ok"):
            return {"compacted": False, "reason": compact_result["reason"]}

        if continuation_prompt:
            continuation_result = await self._drain_message_until_done(
                session,
                continuation_prompt,
                action="web_chat continuation",
            )
            if not continuation_result.get("ok"):
                return {"compacted": False, "reason": continuation_result["reason"]}

        return {"compacted": True}

    async def _drain_message_until_done(
        self,
        session: ChatSessionProtocol,
        message: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        stream: Any | None = None
        try:
            stream = session.send_message(message)
            if inspect.isawaitable(stream):
                stream = await stream

            done_seen = False
            async for event in stream:
                if isinstance(event, DoneEvent):
                    done_seen = True
                    break

            if not done_seen:
                return {
                    "ok": False,
                    "reason": f"{action} stream ended before DoneEvent",
                }
            return {"ok": True}
        except Exception as exc:
            logger.warning("%s failed", action, exc_info=True)
            return {"ok": False, "reason": f"{action} failed: {exc}"}
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    close_result = aclose()
                    if inspect.isawaitable(close_result):
                        await close_result
                except BaseException:
                    pass
