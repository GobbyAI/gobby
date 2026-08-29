"""Wake dispatcher for notifying sessions when async operations complete.

Routes wake messages based on session type after first persisting a durable
InterSessionMessage:
- Terminal agents (agent_depth > 0, terminal_context): tmux send-keys wake signal
- SDK agents (agent_depth > 0, sdk_session_id): SDK resume wake signal
- Interactive sessions (agent_depth 0): managed terminal wake when Gobby owns a
  terminal row for the session, else a tmux pane wake signal
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.agents.tmux.text_injection import TmuxExpectedTextInjectionError
from gobby.sessions.tmux_context import get_tmux_socket_path, parse_terminal_context_value

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

CONTINUE_WAKE_MESSAGE = "Message from Gobby daemon: New activity available."
CONTINUE_WAKE_SIGNAL = f"{CONTINUE_WAKE_MESSAGE}\n"

# Coalesce bursty completions targeting an interactive pane: while the user is
# idle on the same turn, suppress redundant tmux send-keys after the first wake.
# The 30s ceiling guarantees we resume nudging if turn_count signals get missed.
PANE_WAKE_DEBOUNCE_SECONDS = 30.0
# Bounds SDK-resume and web-chat wakes only, which have no internal timeout.
# Never wrap the tmux senders in wait_for: every tmux subprocess is already
# bounded (TmuxTextInjectionTimeout), and an outer cancellation can land
# between paste-buffer and Enter, leaving pasted text unsubmitted.
LIVE_WAKE_TIMEOUT_SECONDS = 5.0

RunDb = Callable[..., Awaitable[Any]]


async def _default_run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


class TmuxSender(Protocol):
    def __call__(
        self,
        identity: str,
        message: str,
        *,
        submit: bool = False,
        escape_before_submit: bool = False,
    ) -> Coroutine[Any, Any, None]: ...


class TmuxPaneSender(Protocol):
    def __call__(
        self,
        pane_id: str,
        message: str,
        tmux_socket_path: str | None,
        *,
        submit: bool = False,
        escape_before_submit: bool = False,
    ) -> Coroutine[Any, Any, None]: ...


class LiveTerminalLookup(Protocol):
    def get_live_for_session(self, session_id: str) -> Any | None: ...


# sdk_resumer signature: (sdk_session_id: str, message: str) -> None
SdkResumer = Callable[[str, str], Coroutine[Any, Any, None]]


class WebChatSessionRegistryProtocol(Protocol):
    async def wake_session(self, session_id: str) -> dict[str, Any]: ...


class WakeDispatcher:
    """Dispatches wake messages to sessions based on their type.

    Constructor args:
        session_manager: For looking up session metadata (agent_depth, terminal_context)
        ism_manager: For creating InterSessionMessages (durable fallback)
        tmux_sender: Optional async callable to send keys to a tmux session
        sdk_resumer: Optional async callable to resume an SDK session with a new prompt
        agent_run_manager: Optional manager for looking up sdk_session_id from agent runs
        terminal_manager: Optional lookup for the live terminal row hosting a session
    """

    def __init__(
        self,
        session_manager: SessionManager,
        ism_manager: InterSessionMessageManager,
        tmux_sender: TmuxSender | None = None,
        tmux_pane_sender: TmuxPaneSender | None = None,
        sdk_resumer: SdkResumer | None = None,
        agent_run_manager: LocalAgentRunManager | None = None,
        web_chat_session_registry: WebChatSessionRegistryProtocol | None = None,
        terminal_manager: LiveTerminalLookup | None = None,
        run_db: RunDb | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._ism_manager = ism_manager
        self._tmux_sender = tmux_sender
        self._tmux_pane_sender = tmux_pane_sender
        self._sdk_resumer = sdk_resumer
        self._agent_run_manager = agent_run_manager
        self._web_chat_session_registry = web_chat_session_registry
        self._terminal_manager = terminal_manager
        self._run_db = run_db or _default_run_db
        # session_id -> (turn_count_at_last_wake, monotonic_ts_at_last_wake)
        self._last_live_wake: dict[str, tuple[int, float]] = {}
        self._live_wake_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def set_web_chat_session_registry(
        self,
        registry: WebChatSessionRegistryProtocol | None,
    ) -> None:
        """Wire the live web-chat registry after server initialization."""
        self._web_chat_session_registry = registry

    def set_terminal_manager(self, manager: LiveTerminalLookup | None) -> None:
        """Wire the terminal row lookup once the composition root has built it."""
        self._terminal_manager = manager

    async def wake(
        self,
        session_id: str,
        message: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Wake a session with a completion notification.

        Args:
            session_id: Target session to wake
            message: Human-readable notification message
            result: Structured result data
        """

        def read_session() -> Any | None:
            with self._session_manager.db.bounded_transaction():
                return self._session_manager.get(session_id)

        session = await self._run_db(read_session)
        if session is None:
            logger.warning("Cannot wake session %s: not found", session_id)
            failure = self._live_wake_failure(
                session_id,
                method=None,
                error_code="session_not_found",
                error_message="Session not found",
            )
            return {**failure, "ism_persisted": False}

        if not await self._send_ism(session_id, message, result):
            failure = self._live_wake_failure(
                session_id,
                method="ism",
                error_code="ism_persist_failed",
                error_message="Could not persist completion notification",
            )
            return {**failure, "ism_persisted": False}

        live_result = await self.dispatch_live_wake(session_id, session=session)
        return {**live_result, "ism_persisted": True}

    async def dispatch_live_wake(
        self,
        session_id: str,
        *,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Send a live wake signal after durable mailbox storage is complete."""
        lock = self._live_wake_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._live_wake_locks[session_id] = lock
        async with lock:
            return await self._dispatch_live_wake_unlocked(session_id, session=session)

    async def _dispatch_live_wake_unlocked(
        self,
        session_id: str,
        *,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Send a live wake signal while holding the per-session wake lock."""
        if session is None:

            def read_session() -> Any | None:
                with self._session_manager.db.bounded_transaction():
                    return self._session_manager.get(session_id)

            session = await self._run_db(read_session)
        if session is None:
            logger.warning("Cannot wake session %s: not found", session_id)
            return {
                "session_id": session_id,
                "delivered": False,
                "method": None,
                "error": "session_not_found",
                "error_code": "session_not_found",
                "error_message": f"Session {session_id} not found",
            }

        agent_depth = getattr(session, "agent_depth", 0) or 0
        terminal_context = getattr(session, "terminal_context", None)
        session_type = getattr(session, "session_type", None)
        status = getattr(session, "status", None)

        if status == "expired":
            return self._live_wake_failure(
                session_id,
                method=None,
                error_code="session_expired",
                error_message=f"Session {session_id} is expired",
            )

        if session_type == "web_chat":
            if not self._should_send_live_wake(session_id, session):
                return self._live_wake_debounced_result(session_id, method="web_chat")
            result = await self._dispatch_web_chat_wake(session_id)
            if result.get("delivered"):
                self._record_live_wake(session_id, session)
            return result

        # Interactive session → nudge its terminal after durable message storage.
        if agent_depth == 0:
            # tmux_pane comes from $TMUX_PANE, so a native/gterm-hosted session
            # never has one and gating this branch on it skipped every native
            # session. The live terminals row is the backend-neutral gate; the
            # raw pane stays the fallback for a tmux session Gobby holds no row
            # for, where there is no backend to resolve a runtime from.
            terminal = await self._live_terminal_for_session(session_id)
            if terminal is not None and self._tmux_sender is not None:
                if not self._should_send_live_wake(session_id, session):
                    return self._live_wake_debounced_result(session_id, method="terminal")
                return await self._send_managed_terminal_wake(
                    session_id,
                    session,
                    terminal,
                    self._tmux_sender,
                )
            if not terminal_context:
                return self._live_wake_failure(
                    session_id,
                    method=None,
                    error_code="no_live_wake_channel",
                    error_message="Session has no terminal_context for live wake",
                )
            tmux_pane = self._parse_tmux_pane(terminal_context)
            if not tmux_pane:
                return self._live_wake_failure(
                    session_id,
                    method="tmux_pane",
                    error_code="no_tmux_pane",
                    error_message="Session terminal_context has no tmux_pane",
                )
            if not self._tmux_pane_sender:
                return self._live_wake_failure(
                    session_id,
                    method="tmux_pane",
                    error_code="no_live_wake_channel",
                    error_message="No tmux pane sender is configured",
                )
            if not self._should_send_live_wake(session_id, session):
                return self._live_wake_debounced_result(session_id, method="tmux_pane")
            tmux_socket_path = self._parse_tmux_socket_path(terminal_context)
            try:
                await self._tmux_pane_sender(
                    tmux_pane,
                    CONTINUE_WAKE_MESSAGE,
                    tmux_socket_path,
                    submit=True,
                    escape_before_submit=True,
                )
                self._record_live_wake(session_id, session)
                return {
                    "session_id": session_id,
                    "delivered": True,
                    "method": "tmux_pane",
                }
            except TmuxExpectedTextInjectionError as exc:
                detail = str(exc) or type(exc).__name__
                logger.info(
                    "tmux pane wake skipped for session %s (pane=%s): %s",
                    session_id,
                    tmux_pane,
                    detail,
                )
                return self._live_wake_failure(
                    session_id,
                    method="tmux_pane",
                    error_code="tmux_pane_wake_failed",
                    error_message=detail,
                )
            except Exception as exc:
                detail = str(exc) or type(exc).__name__
                logger.warning(
                    "tmux pane wake failed for session %s (pane=%s)",
                    session_id,
                    tmux_pane,
                    exc_info=True,
                )
                return self._live_wake_failure(
                    session_id,
                    method="tmux_pane",
                    error_code="tmux_pane_wake_failed",
                    error_message=detail,
                )

        # Terminal agent → try tmux, then SDK. Both are wake signals only.
        if not self._should_send_live_wake(session_id, session):
            return self._live_wake_debounced_result(session_id, method="live_wake")

        if terminal_context and self._tmux_sender:
            wake_identity = self._parse_tmux_session(terminal_context)
            if wake_identity:
                try:
                    await self._tmux_sender(
                        wake_identity,
                        CONTINUE_WAKE_MESSAGE,
                        submit=True,
                        escape_before_submit=True,
                    )
                    self._record_live_wake(session_id, session)
                    return {
                        "session_id": session_id,
                        "delivered": True,
                        "method": "tmux",
                    }
                except Exception as exc:
                    from gobby.terminals.runtime import IndeterminateWrite

                    if isinstance(exc, IndeterminateWrite):
                        return {
                            "session_id": session_id,
                            "delivered": False,
                            "method": "tmux",
                            "indeterminate": True,
                            "error_message": exc.detail,
                        }
                    logger.warning(
                        "tmux wake failed for session %s (tmux=%s), trying SDK resume",
                        session_id,
                        wake_identity,
                        exc_info=True,
                    )

        if terminal_context and self._tmux_pane_sender:
            tmux_pane = self._parse_tmux_pane(terminal_context)
            if tmux_pane:
                tmux_socket_path = self._parse_tmux_socket_path(terminal_context)
                try:
                    await self._tmux_pane_sender(
                        tmux_pane,
                        CONTINUE_WAKE_MESSAGE,
                        tmux_socket_path,
                        submit=True,
                        escape_before_submit=True,
                    )
                    self._record_live_wake(session_id, session)
                    return {
                        "session_id": session_id,
                        "delivered": True,
                        "method": "tmux_pane",
                    }
                except TmuxExpectedTextInjectionError as exc:
                    logger.info(
                        "tmux pane wake skipped for terminal agent session %s (pane=%s), "
                        "trying SDK resume: %s",
                        session_id,
                        tmux_pane,
                        str(exc) or type(exc).__name__,
                    )
                except Exception:
                    logger.warning(
                        "tmux pane wake failed for terminal agent session %s (pane=%s), "
                        "trying SDK resume",
                        session_id,
                        tmux_pane,
                        exc_info=True,
                    )

        # SDK agent → try resume via sdk_session_id
        if self._sdk_resumer:
            sdk_session_id = await self._resolve_sdk_session_id(session_id)
            if sdk_session_id:
                try:
                    await asyncio.wait_for(
                        self._sdk_resumer(sdk_session_id, CONTINUE_WAKE_SIGNAL),
                        timeout=LIVE_WAKE_TIMEOUT_SECONDS,
                    )
                    self._record_live_wake(session_id, session)
                    return {
                        "session_id": session_id,
                        "delivered": True,
                        "method": "sdk",
                    }
                except Exception:
                    logger.warning(
                        "SDK resume failed for session %s (sdk=%s)",
                        session_id,
                        sdk_session_id,
                        exc_info=True,
                    )
                    return {
                        "session_id": session_id,
                        "delivered": False,
                        "method": "sdk",
                        "error": "sdk_resume_failed",
                        "error_code": "sdk_resume_failed",
                        "error_message": "SDK resume failed",
                    }

        return self._live_wake_failure(
            session_id,
            method=None,
            error_code="no_live_wake_channel",
            error_message="No live wake channel is available for this session",
        )

    async def _live_terminal_for_session(self, session_id: str) -> Any | None:
        """Resolve the managed terminal row hosting this session, if Gobby owns one."""
        manager = self._terminal_manager
        if manager is None:
            return None

        def read_terminal() -> Any | None:
            return manager.get_live_for_session(session_id)

        try:
            return await self._run_db(read_terminal)
        except Exception:
            logger.warning(
                "live terminal lookup failed for session %s",
                session_id,
                exc_info=True,
            )
            return None

    async def _send_managed_terminal_wake(
        self,
        session_id: str,
        session: Any,
        terminal: Any,
        send: TmuxSender,
    ) -> dict[str, Any]:
        """Wake a session through the terminal row that hosts it.

        `send` is the composition root's wake sender: it resolves the row by
        identity and writes through the write coordinator, so the runtime comes
        from Terminal.backend and native rows are driven as well as tmux ones.
        """
        from gobby.terminals.runtime import IndeterminateWrite

        terminal_id = str(terminal.id)
        try:
            await send(
                terminal_id,
                CONTINUE_WAKE_MESSAGE,
                submit=True,
                escape_before_submit=True,
            )
        except IndeterminateWrite as exc:
            # Bytes may already be on screen, so record no delivery and try no
            # other route: a second wake would double-write the same terminal.
            return {
                "session_id": session_id,
                "delivered": False,
                "method": "terminal",
                "indeterminate": True,
                "error_message": exc.detail,
            }
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            logger.warning(
                "terminal wake failed for session %s (terminal=%s)",
                session_id,
                terminal_id,
                exc_info=True,
            )
            return self._live_wake_failure(
                session_id,
                method="terminal",
                error_code="terminal_wake_failed",
                error_message=detail,
            )
        self._record_live_wake(session_id, session)
        return {
            "session_id": session_id,
            "delivered": True,
            "method": "terminal",
        }

    @staticmethod
    def _live_wake_failure(
        session_id: str,
        *,
        method: str | None,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "delivered": False,
            "method": method,
            "error": error_code,
            "error_code": error_code,
            "error_message": error_message,
        }

    async def _dispatch_web_chat_wake(self, session_id: str) -> dict[str, Any]:
        if self._web_chat_session_registry is None:
            return self._web_chat_no_live_result(session_id)

        try:
            result = await asyncio.wait_for(
                self._web_chat_session_registry.wake_session(session_id),
                timeout=LIVE_WAKE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "web_chat wake failed for session %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            return {
                "session_id": session_id,
                "delivered": False,
                "method": "web_chat",
                "error": str(exc),
                "error_code": "web_chat_wake_failed",
                "error_message": str(exc),
            }

        if not isinstance(result, dict):
            return {
                "session_id": session_id,
                "delivered": False,
                "method": "web_chat",
                "error_code": "web_chat_wake_failed",
            }
        result.setdefault("session_id", session_id)
        result.setdefault("method", "web_chat")
        return result

    @staticmethod
    def _live_wake_debounced_result(session_id: str, *, method: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "delivered": False,
            "method": method,
            "skipped": "debounced",
        }

    @staticmethod
    def _web_chat_no_live_result(session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "delivered": False,
            "method": "web_chat",
            "error": "no_live_web_chat_session",
            "error_code": "no_live_web_chat_session",
            "error_message": f"No live web_chat session found for {session_id}",
        }

    def _prune_live_wake_state(self, stale_before: float) -> None:
        """Drop stale wake timestamps and unused per-session locks."""
        for recorded_session_id, (_, recorded_ts) in tuple(self._last_live_wake.items()):
            if recorded_ts >= stale_before:
                continue
            lock = self._live_wake_locks.get(recorded_session_id)
            if lock is not None and lock.locked():
                continue
            self._last_live_wake.pop(recorded_session_id, None)
            self._live_wake_locks.pop(recorded_session_id, None)

    def _should_send_live_wake(self, session_id: str, session: Any) -> bool:
        """Decide whether to send a live wake signal to a session.

        Coalesces bursty completions: if a wake was already delivered to
        this session and the user has not advanced the turn since (and the 30s
        ceiling has not elapsed), skip the live nudge. Durable ISMs are stored
        unconditionally, so the agent still sees every completion when it next
        reads its inbox.
        """
        now = time.monotonic()
        self._prune_live_wake_state(now - PANE_WAKE_DEBOUNCE_SECONDS)

        last = self._last_live_wake.get(session_id)
        if last is None:
            return True
        last_turn, last_ts = last
        current_turn = int(getattr(session, "turn_count", 0) or 0)
        if current_turn > last_turn:
            return True
        return (now - last_ts) >= PANE_WAKE_DEBOUNCE_SECONDS

    def _record_live_wake(self, session_id: str, session: Any) -> None:
        """Record that a live wake was just delivered to this session."""
        current_turn = int(getattr(session, "turn_count", 0) or 0)
        self._last_live_wake[session_id] = (current_turn, time.monotonic())

    async def _resolve_sdk_session_id(self, session_id: str) -> str | None:
        """Look up the SDK session ID for a session via agent_runs.

        Checks if the session is a child of an agent run that captured
        an sdk_session_id during execution.
        """
        agent_run_manager = self._agent_run_manager
        if agent_run_manager is None:
            return None
        try:

            def resolve_sdk_session_id() -> str | None:
                with agent_run_manager.db.bounded_transaction():
                    session = self._session_manager.get(session_id)
                    if session and getattr(session, "external_id", None):
                        return cast(str | None, session.external_id)
                    return agent_run_manager.get_sdk_session_id_for_session(session_id)

            return cast(str | None, await self._run_db(resolve_sdk_session_id))
        except Exception:
            logger.debug(
                "Could not resolve sdk_session_id for session %s",
                session_id,
                exc_info=True,
            )
            return None

    async def _send_ism(
        self,
        session_id: str,
        message: str,
        result: dict[str, Any],
    ) -> bool:
        """Send an InterSessionMessage as durable notification."""

        def persist_notification() -> bool:
            content = str(
                result.get("signoff_message")
                or result.get("continuation_prompt")
                or message
                or "Completion available"
            )
            from_session = str(result.get("from_session_id") or session_id)
            message_type = str(result.get("message_type") or "completion_notification")
            metadata = {**result, "completion_message": message}
            completion_id = self._notification_completion_id(metadata)
            if completion_id and "completion_id" not in metadata:
                metadata["completion_id"] = completion_id
            with self._ism_manager.db.bounded_transaction():
                if completion_id and self._notification_exists(
                    session_id,
                    message_type,
                    completion_id,
                ):
                    return True
                self._ism_manager.create_message(
                    from_session=from_session,
                    to_session=session_id,
                    content=content,
                    message_type=message_type,
                    priority="high",
                    metadata_json=json.dumps(metadata, default=str, sort_keys=True),
                )
            return True

        try:
            return bool(await self._run_db(persist_notification))
        except Exception:
            logger.exception(
                "Failed to send ISM to session %s",
                session_id,
            )
            return False

    def _notification_exists(
        self,
        session_id: str,
        message_type: str,
        completion_id: str,
    ) -> bool:
        """Return True if this durable completion notification already exists."""
        has_notification = getattr(
            type(self._ism_manager),
            "has_completion_notification",
            None,
        )
        if callable(has_notification):
            try:
                return bool(
                    has_notification(self._ism_manager, session_id, message_type, completion_id)
                )
            except Exception:
                logger.debug(
                    "Could not query existing completion notification for %s",
                    session_id,
                    exc_info=True,
                )

        list_messages = getattr(self._ism_manager, "list_messages", None)
        if not callable(list_messages):
            return False
        try:
            messages = list_messages(
                session_id,
                direction="inbox",
                message_type=message_type,
                limit=100,
            )
        except Exception:
            logger.debug(
                "Could not query existing completion notifications for %s",
                session_id,
                exc_info=True,
            )
            return False

        for msg in messages:
            metadata_json = getattr(msg, "metadata_json", None)
            if not metadata_json:
                continue
            try:
                metadata = json.loads(metadata_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if self._notification_completion_id(metadata) == completion_id:
                return True
        return False

    @staticmethod
    def _notification_completion_id(metadata: dict[str, Any]) -> str | None:
        """Resolve the stable id used to dedupe completion notifications."""
        value = (
            metadata.get("completion_id") or metadata.get("run_id") or metadata.get("execution_id")
        )
        return str(value) if value else None

    @staticmethod
    def _parse_tmux_session(terminal_context: Any) -> str | None:
        """Extract tmux session name from terminal_context JSON."""
        ctx = parse_terminal_context_value(terminal_context)
        if not ctx:
            return None
        value = ctx.get("tmux_session")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _parse_tmux_pane(terminal_context: Any) -> str | None:
        """Extract tmux pane ID from terminal_context JSON."""
        ctx = parse_terminal_context_value(terminal_context)
        if not ctx:
            return None
        value = ctx.get("tmux_pane")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _parse_tmux_socket_path(terminal_context: Any) -> str | None:
        """Extract tmux socket path from terminal_context JSON."""
        ctx = parse_terminal_context_value(terminal_context)
        if not ctx:
            return None
        return get_tmux_socket_path(ctx)
