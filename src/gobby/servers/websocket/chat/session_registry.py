"""Daemon-owned registry for live web-chat sessions."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Protocol

from gobby.llm.claude_models import DoneEvent
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.sessions.clear_continuation import clear_failed_attempt
from gobby.sessions.handoff import build_handoff_continue_prompt, restore_staged_handoff

logger = logging.getLogger(__name__)

WEB_CHAT_WAKE_PROMPT = "Message from Gobby daemon: New activity available."


class ClearLifecycleHooks(Protocol):
    """Typed seam from the live registry to chat-layer session-boundary work."""

    async def commit_clear_successor(
        self,
        *,
        conversation_id: str,
        session: ChatSessionProtocol,
        predecessor_id: str,
        attempt_id: str,
    ) -> dict[str, Any]: ...


class WebChatSessionRegistry:
    """Shared live-session registry used by WebSocket chat and MCP tools."""

    def __init__(self) -> None:
        self.sessions: dict[str, ChatSessionProtocol] = {}
        self.active_tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_compactions: dict[str, tuple[str, str | None]] = {}
        self._queued_compaction_tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_wakes: dict[str, tuple[str, str]] = {}
        self._queued_wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_clears: dict[str, str] = {}
        self._queued_clear_tasks: dict[str, asyncio.Task[None]] = {}
        self._clear_hooks: ClearLifecycleHooks | None = None
        self._clear_db: Any | None = None

    def bind_clear_lifecycle(
        self,
        hooks: ClearLifecycleHooks,
        *,
        db: Any | None = None,
    ) -> None:
        """Bind the commit seam owned by the WebSocket server."""
        self._clear_hooks = hooks
        self._clear_db = db

    def register(self, conversation_id: str, session: ChatSessionProtocol) -> None:
        """Register a live chat session by conversation id."""
        self.sessions[conversation_id] = session

    def unregister(self, conversation_id: str) -> None:
        """Remove a session and any queued registry state."""
        session = self.sessions.get(conversation_id)
        attempt_id = self._queued_clears.pop(conversation_id, None)
        if attempt_id is not None:
            predecessor_id = getattr(session, "db_session_id", None)
            if not isinstance(predecessor_id, str) or not predecessor_id:
                predecessor_id = conversation_id
            self._fail_clear_attempt(predecessor_id, attempt_id)
        compact_request = self._queued_compactions.pop(conversation_id, None)
        if compact_request is not None and compact_request[1] is not None:
            predecessor_id = getattr(session, "db_session_id", None) or conversation_id
            self._fail_handoff_attempt(str(predecessor_id), compact_request[1])
        self.sessions.pop(conversation_id, None)
        self.active_tasks.pop(conversation_id, None)
        self._queued_wakes.pop(conversation_id, None)
        queued_task = self._queued_compaction_tasks.pop(conversation_id, None)
        if queued_task is not None and not queued_task.done():
            queued_task.cancel()
        queued_wake_task = self._queued_wake_tasks.pop(conversation_id, None)
        if queued_wake_task is not None and not queued_wake_task.done():
            queued_wake_task.cancel()
        queued_clear_task = self._queued_clear_tasks.pop(conversation_id, None)
        if queued_clear_task is not None and not queued_clear_task.done():
            queued_clear_task.cancel()

    def clear(self) -> None:
        """Clear all live registry state."""
        for conversation_id, attempt_id in list(self._queued_clears.items()):
            session = self.sessions.get(conversation_id)
            predecessor_id = getattr(session, "db_session_id", None)
            if not isinstance(predecessor_id, str) or not predecessor_id:
                predecessor_id = conversation_id
            self._fail_clear_attempt(predecessor_id, attempt_id)
        for conversation_id, (_, compact_attempt_id) in list(self._queued_compactions.items()):
            if compact_attempt_id is None:
                continue
            session = self.sessions.get(conversation_id)
            predecessor_value = getattr(session, "db_session_id", None) or conversation_id
            self._fail_handoff_attempt(str(predecessor_value), compact_attempt_id)
        for task in self._queued_compaction_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._queued_wake_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._queued_clear_tasks.values():
            if not task.done():
                task.cancel()
        self.sessions.clear()
        self.active_tasks.clear()
        self._queued_compactions.clear()
        self._queued_compaction_tasks.clear()
        self._queued_wakes.clear()
        self._queued_wake_tasks.clear()
        self._queued_clears.clear()
        self._queued_clear_tasks.clear()

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

    async def clear_session(
        self,
        session_id: str,
        *,
        attempt_id: str,
        continuation_prompt: str,
    ) -> dict[str, Any]:
        """Prepare/clear/commit a live web-chat session onto a force-new successor."""
        conversation_id, session = self.find_session(session_id)
        if conversation_id is None or session is None:
            return {
                "cleared": False,
                "reason": f"No live web_chat session found for {session_id}",
            }

        pending_attempt = self._queued_clears.get(conversation_id)
        if pending_attempt is not None:
            return {"queued": True, "attempt_id": pending_attempt}

        if self.has_active_turn(conversation_id) or self._has_blocking_queued_task(conversation_id):
            self._queued_clears[conversation_id] = attempt_id
            return {"queued": True, "attempt_id": attempt_id}

        return await self._execute_clear_session(
            conversation_id,
            session,
            session_id=session_id,
            attempt_id=attempt_id,
            continuation_prompt=continuation_prompt,
        )

    async def _execute_clear_session(
        self,
        conversation_id: str,
        session: ChatSessionProtocol,
        *,
        session_id: str,
        attempt_id: str,
        continuation_prompt: str,
    ) -> dict[str, Any]:
        predecessor_id = getattr(session, "db_session_id", None)
        if not isinstance(predecessor_id, str) or not predecessor_id:
            predecessor_id = session_id

        try:
            cleared = await session.clear_context()
        except Exception:
            logger.warning(
                "web_chat clear_context failed for %s",
                predecessor_id,
                exc_info=True,
            )
            cleared = False
        if cleared is not True:
            self._fail_clear_attempt(predecessor_id, attempt_id)
            return {
                "cleared": False,
                "queued": False,
                "attempt_id": attempt_id,
                "reason": "clear_context failed",
            }

        hooks = self._clear_hooks
        if hooks is None:
            self._fail_clear_attempt(predecessor_id, attempt_id)
            return {
                "cleared": False,
                "queued": False,
                "attempt_id": attempt_id,
                "reason": "clear lifecycle hooks are not bound",
            }

        try:
            commit_result = await hooks.commit_clear_successor(
                conversation_id=conversation_id,
                session=session,
                predecessor_id=predecessor_id,
                attempt_id=attempt_id,
            )
        except Exception:
            logger.exception("web_chat clear commit failed for %s", predecessor_id)
            self._fail_clear_attempt(predecessor_id, attempt_id)
            return {
                "cleared": False,
                "queued": False,
                "attempt_id": attempt_id,
                "reason": "clear successor commit failed",
            }
        if not commit_result.get("ok"):
            self._fail_clear_attempt(predecessor_id, attempt_id)
            return {
                "cleared": False,
                "queued": False,
                "attempt_id": attempt_id,
                "reason": str(commit_result.get("reason") or "clear successor commit failed"),
            }

        continuation = await self._drain_message_until_done(
            session,
            continuation_prompt,
            action="web_chat clear continuation",
        )
        result: dict[str, Any] = {
            "cleared": True,
            "queued": False,
            "attempt_id": attempt_id,
            "predecessor_id": predecessor_id,
            "successor_id": commit_result.get("successor_id"),
            "via": "web_chat",
        }
        if not continuation.get("ok"):
            result["continuation_reason"] = continuation.get("reason")
        return result

    def _fail_clear_attempt(self, session_id: str, attempt_id: str) -> None:
        db = self._clear_db
        if db is None:
            logger.error(
                "Cannot fail clear attempt %s for %s: no database is bound",
                attempt_id,
                session_id,
            )
            return
        clear_failed_attempt(db, session_id, attempt_id=attempt_id)

    def _fail_handoff_attempt(self, session_id: str, attempt_id: str) -> None:
        db = self._clear_db
        if db is None:
            logger.error(
                "Cannot fail handoff attempt %s for %s: no database is bound",
                attempt_id,
                session_id,
            )
            return
        restore_staged_handoff(db, session_id, attempt_id)

    async def compact_session(
        self,
        session_id: str,
        command: str = "/compact",
        handoff_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Trigger a web-chat compaction command on a live session."""
        conversation_id, session = self.find_session(session_id)
        if conversation_id is None or session is None:
            return {
                "compacted": False,
                "reason": f"No live web_chat session found for {session_id}",
            }

        if self.has_active_turn(conversation_id):
            self._queued_compactions[conversation_id] = (command, handoff_attempt_id)
            return {
                "compacted": True,
                "command": command,
                "via": "web_chat",
                "queued": True,
            }

        result = await self._drain_compaction(
            session,
            command,
            continuation_prompt=build_handoff_continue_prompt(),
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
        compact_request = self._queued_compactions.pop(conversation_id, None)
        command = compact_request[0] if compact_request is not None else None
        compact_attempt_id = compact_request[1] if compact_request is not None else None
        wake_request = self._queued_wakes.pop(conversation_id, None)
        clear_attempt_id = self._queued_clears.pop(conversation_id, None)
        if command is None and wake_request is None and clear_attempt_id is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Cannot run queued web_chat work without an event loop")
            if clear_attempt_id is not None:
                session = self.sessions.get(conversation_id)
                predecessor_id = getattr(session, "db_session_id", None)
                if not isinstance(predecessor_id, str) or not predecessor_id:
                    predecessor_id = conversation_id
                self._fail_clear_attempt(predecessor_id, clear_attempt_id)
            if compact_attempt_id is not None:
                session = self.sessions.get(conversation_id)
                predecessor_id = getattr(session, "db_session_id", None) or conversation_id
                self._fail_handoff_attempt(str(predecessor_id), compact_attempt_id)
            return

        queued_task = loop.create_task(
            self._run_queued_after_turn(
                conversation_id,
                command,
                compact_attempt_id,
                wake_request,
                clear_attempt_id,
            )
        )
        if compact_request is not None:
            self._queued_compaction_tasks[conversation_id] = queued_task
            queued_task.add_done_callback(
                lambda done_task: self._on_queued_compaction_done(
                    conversation_id,
                    done_task,
                    compact_attempt_id,
                )
            )
        if wake_request is not None:
            self._queued_wake_tasks[conversation_id] = queued_task
            queued_task.add_done_callback(
                lambda done_task: self._on_queued_wake_done(conversation_id, done_task)
            )
        if clear_attempt_id is not None:
            self._queued_clear_tasks[conversation_id] = queued_task
            queued_task.add_done_callback(
                lambda done_task: self._on_queued_clear_done(conversation_id, done_task)
            )

    def _on_queued_compaction_done(
        self,
        conversation_id: str,
        task: asyncio.Task[None],
        attempt_id: str | None = None,
    ) -> None:
        self._queued_compaction_tasks.pop(conversation_id, None)
        if task.cancelled():
            if attempt_id is not None:
                _, session = self.find_session(conversation_id)
                predecessor_id = getattr(session, "db_session_id", None) or conversation_id
                self._fail_handoff_attempt(str(predecessor_id), attempt_id)
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Queued web_chat compaction failed for %s",
                conversation_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if attempt_id is not None:
                _, session = self.find_session(conversation_id)
                predecessor_id = getattr(session, "db_session_id", None) or conversation_id
                self._fail_handoff_attempt(str(predecessor_id), attempt_id)
            return
        self._schedule_queued_clear_if_idle(conversation_id)
        self._schedule_queued_wake_if_idle(conversation_id)

    def _on_queued_clear_done(
        self,
        conversation_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._queued_clear_tasks.pop(conversation_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Queued web_chat clear failed for %s",
                conversation_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        self._schedule_queued_wake_if_idle(conversation_id)

    async def _run_queued_compaction(self, conversation_id: str, command: str) -> None:
        await self._run_queued_after_turn(conversation_id, command, None, None)

    async def _run_queued_after_turn(
        self,
        conversation_id: str,
        command: str | None,
        compact_attempt_id: str | None,
        wake_request: tuple[str, str] | None,
        clear_attempt_id: str | None = None,
    ) -> None:
        if self.has_active_turn(conversation_id):
            if command is not None:
                self._queued_compactions[conversation_id] = (command, compact_attempt_id)
            if wake_request is not None:
                self._queued_wakes[conversation_id] = wake_request
            if clear_attempt_id is not None:
                self._queued_clears[conversation_id] = clear_attempt_id
            return

        if clear_attempt_id is not None:
            _, session = self.find_session(conversation_id)
            if session is None:
                logger.warning(
                    "Queued web_chat clear failed for %s: no live session",
                    conversation_id,
                )
                self._fail_clear_attempt(conversation_id, clear_attempt_id)
            else:
                predecessor_id = getattr(session, "db_session_id", None)
                if not isinstance(predecessor_id, str) or not predecessor_id:
                    predecessor_id = conversation_id
                result = await self._execute_clear_session(
                    conversation_id,
                    session,
                    session_id=conversation_id,
                    attempt_id=clear_attempt_id,
                    continuation_prompt=build_handoff_continue_prompt(),
                )
                if not result.get("cleared") and not result.get("queued"):
                    logger.warning(
                        "Queued web_chat clear failed for %s: %s",
                        conversation_id,
                        result.get("reason", "unknown error"),
                    )
            command = None

        if command is not None:
            result = await self.compact_session(
                conversation_id,
                command=command,
                handoff_attempt_id=compact_attempt_id,
            )
            if not result.get("compacted"):
                logger.warning(
                    "Queued web_chat compaction failed for %s: %s",
                    conversation_id,
                    result.get("reason", "unknown error"),
                )
                if compact_attempt_id is not None:
                    _, session = self.find_session(conversation_id)
                    predecessor_id = getattr(session, "db_session_id", None) or conversation_id
                    self._fail_handoff_attempt(str(predecessor_id), compact_attempt_id)

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
        self._schedule_queued_clear_if_idle(conversation_id)
        self._schedule_queued_wake_if_idle(conversation_id)

    def _schedule_queued_clear_if_idle(self, conversation_id: str) -> None:
        if self.has_active_turn(conversation_id) or self._has_running_queued_task(conversation_id):
            return
        attempt_id = self._queued_clears.pop(conversation_id, None)
        if attempt_id is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._queued_clears[conversation_id] = attempt_id
            logger.warning("Cannot run queued web_chat clear without an event loop")
            return
        queued_task = loop.create_task(
            self._run_queued_after_turn(conversation_id, None, None, None, attempt_id)
        )
        self._queued_clear_tasks[conversation_id] = queued_task
        queued_task.add_done_callback(
            lambda done_task: self._on_queued_clear_done(conversation_id, done_task)
        )

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
            self._run_queued_after_turn(conversation_id, None, None, wake_request)
        )
        self._queued_wake_tasks[conversation_id] = queued_task
        queued_task.add_done_callback(
            lambda done_task: self._on_queued_wake_done(conversation_id, done_task)
        )

    def _has_running_queued_task(self, conversation_id: str) -> bool:
        tasks = (
            self._queued_compaction_tasks.get(conversation_id),
            self._queued_wake_tasks.get(conversation_id),
            self._queued_clear_tasks.get(conversation_id),
        )
        return any(task is not None and not task.done() for task in tasks)

    def _has_blocking_queued_task(self, conversation_id: str) -> bool:
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
                except Exception:
                    pass
