"""Wake dispatcher for notifying sessions when async operations complete.

Routes wake messages based on session type after first persisting a durable
InterSessionMessage:
- Any session Gobby owns a live terminal row for: managed terminal wake through
  that row, whatever its backend (tmux or native)
- Terminal agents without a row (agent_depth > 0, terminal_context): tmux wake signal
- SDK agents (agent_depth > 0, sdk_session_id): SDK resume wake signal
- Interactive sessions without a row (agent_depth 0): tmux pane wake signal
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.agents.tmux.text_injection import TmuxExpectedTextInjectionError
from gobby.events.live_wake import (
    ComposerProbe,
    composer_occupied_result,
    normalize_live_wake_result,
    wake_debounced_result,
    wake_failure,
    wake_state_failure,
)
from gobby.events.wake_notifications import persist_completion_notification
from gobby.events.wake_terminal_resolution import (
    LiveTerminalResolver,
    SessionTerminalRoute,
    resolve_session_terminal_route,
)

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
LifecycleRefresh = Callable[[str], Awaitable[None]]


async def _default_run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


class TmuxSender(Protocol):
    def __call__(
        self,
        identity: str,
        message: str,
        *,
        submit: bool = False,
        clear_before_submit: bool = False,
        cli_source: str | None = None,
    ) -> Coroutine[Any, Any, None]: ...


class TmuxPaneSender(Protocol):
    def __call__(
        self,
        pane_id: str,
        message: str,
        tmux_socket_path: str | None,
        *,
        submit: bool = False,
        clear_before_submit: bool = False,
        cli_source: str | None = None,
    ) -> Coroutine[Any, Any, None]: ...


# sdk_resumer signature: (sdk_session_id: str, message: str) -> None
SdkResumer = Callable[[str, str], Coroutine[Any, Any, None]]


class WebChatSessionRegistryProtocol(Protocol):
    async def wake_session(self, session_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NativeWakeTarget:
    session_id: str
    terminal_id: str
    cli_source: str | None


class NativeBatchSender(Protocol):
    def __call__(
        self, targets: list[NativeWakeTarget]
    ) -> Coroutine[Any, Any, list[dict[str, Any]]]: ...


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
        native_batch_sender: NativeBatchSender | None = None,
        sdk_resumer: SdkResumer | None = None,
        agent_run_manager: LocalAgentRunManager | None = None,
        web_chat_session_registry: WebChatSessionRegistryProtocol | None = None,
        terminal_manager: LiveTerminalResolver | None = None,
        run_db: RunDb | None = None,
        lifecycle_refresh: LifecycleRefresh | None = None,
        composer_probe: ComposerProbe | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._ism_manager = ism_manager
        self._tmux_sender = tmux_sender
        self._tmux_pane_sender = tmux_pane_sender
        self._native_batch_sender = native_batch_sender
        self._sdk_resumer = sdk_resumer
        self._agent_run_manager = agent_run_manager
        self._web_chat_session_registry = web_chat_session_registry
        self._terminal_manager = terminal_manager
        self._run_db = run_db or _default_run_db
        self._lifecycle_refresh = lifecycle_refresh
        self._composer_probe = composer_probe
        # session_id -> (turn_count_at_last_wake, monotonic_ts_at_last_wake)
        self._last_live_wake: dict[str, tuple[int, float]] = {}
        self._live_wake_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    def bind_owner_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Confine wakes to the daemon loop that owns the per-session locks."""
        self._owner_loop = loop

    def _require_owner_loop(self) -> None:
        # Terminal delivery hands foreign-loop callers to the owner loop before waking.
        if self._owner_loop is not None and asyncio.get_running_loop() is not self._owner_loop:
            raise RuntimeError("Wake dispatch must run on the daemon event loop")

    def set_web_chat_session_registry(
        self,
        registry: WebChatSessionRegistryProtocol | None,
    ) -> None:
        """Wire the live web-chat registry after server initialization."""
        self._web_chat_session_registry = registry

    def set_terminal_manager(self, manager: LiveTerminalResolver | None) -> None:
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
        self._require_owner_loop()

        def read_session() -> Any | None:
            with self._session_manager.db.bounded_transaction():
                return self._session_manager.get(session_id)

        session = await self._run_db(read_session)
        if session is None:
            logger.warning("Cannot wake session %s: not found", session_id)
            failure = wake_failure(
                session_id,
                method=None,
                error_code="session_not_found",
                error_message="Session not found",
            )
            return {**failure, "ism_persisted": False}

        if not await persist_completion_notification(
            self._ism_manager,
            self._run_db,
            session_id,
            message,
            result,
        ):
            failure = wake_failure(
                session_id,
                method="ism",
                error_code="ism_persist_failed",
                error_message="Could not persist completion notification",
            )
            return {**failure, "ism_persisted": False}

        priority = str(result.get("priority") or "normal")
        live_result = await self.dispatch_live_wake(session_id, priority=priority)
        return {**live_result, "ism_persisted": True}

    async def dispatch_live_wake(
        self,
        session_id: str,
        *,
        priority: str = "normal",
    ) -> dict[str, Any]:
        """Send a live wake signal after durable mailbox storage is complete."""
        self._require_owner_loop()
        lock = self._live_wake_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._live_wake_locks[session_id] = lock
        async with lock:
            if self._lifecycle_refresh is not None:
                try:
                    await self._lifecycle_refresh(session_id)
                except Exception:
                    logger.warning(
                        "Lifecycle refresh failed before waking session %s",
                        session_id,
                        exc_info=True,
                    )
            result = await self._dispatch_live_wake_unlocked(session_id, priority=priority)
            return normalize_live_wake_result(result)

    async def dispatch_live_wakes(
        self,
        session_ids: list[str],
        *,
        priority: str = "normal",
    ) -> list[dict[str, Any]]:
        """Batch eligible native-terminal wakes and preserve recipient order."""
        self._require_owner_loop()
        from gobby.events.wake_batch import dispatch_live_wakes

        results = await dispatch_live_wakes(self, session_ids, priority=priority)
        return [normalize_live_wake_result(result) for result in results]

    async def _dispatch_live_wake_unlocked(
        self,
        session_id: str,
        *,
        session: Any | None = None,
        priority: str = "normal",
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

        # Context-capable hooks inject the durable message on the next model call.
        # Submitting terminal input while a provider is active would steer that turn
        # and cancel its in-flight tool batch. Check after acquiring the lock and
        # refreshing lifecycle state for both mailbox and completion wakes.
        if getattr(session, "status", None) == "active" and priority != "urgent":
            return {
                "session_id": session_id,
                "delivered": False,
                "method": "next_call_context",
                "skipped": "session_active",
                "ism_persisted": True,
            }

        agent_depth = getattr(session, "agent_depth", 0) or 0
        session_type = getattr(session, "session_type", None)
        status = getattr(session, "status", None)
        state_failure = wake_state_failure(session_id, status)
        if state_failure is not None:
            return state_failure

        if session_type == "web_chat":
            if not self._should_send_live_wake(session_id, session):
                return wake_debounced_result(session_id, method="web_chat")
            result = await self._dispatch_web_chat_wake(session_id)
            if result.get("delivered"):
                self._record_live_wake(session_id, session)
            return result

        # tmux_pane and tmux_session come from tmux, so a native/gterm-hosted
        # session never has either and gating on them skipped every native
        # session, interactive or spawned. The live terminals row is the
        # backend-neutral gate; the raw tmux keys and SDK resume stay the
        # fallbacks for a session Gobby holds no row for, where there is no
        # backend to resolve a runtime from.
        terminal_route = await self._terminal_route_for_session(session)
        terminal = terminal_route.managed_terminal
        if terminal is not None and self._tmux_sender is not None:
            if not self._should_send_live_wake(session_id, session):
                return wake_debounced_result(session_id, method="terminal")
            return await self._send_managed_terminal_wake(
                session_id,
                session,
                terminal,
                self._tmux_sender,
                priority=priority,
            )

        # Interactive session → nudge its tmux pane after durable message storage.
        if agent_depth == 0:
            if not terminal_route.has_terminal_context:
                return wake_failure(
                    session_id,
                    method=None,
                    error_code="no_live_wake_channel",
                    error_message="Session has no terminal_context for live wake",
                )
            tmux_pane = terminal_route.tmux_pane
            if not tmux_pane:
                return wake_failure(
                    session_id,
                    method="tmux_pane",
                    error_code="no_tmux_pane",
                    error_message="Session terminal_context has no tmux_pane",
                )
            if not self._tmux_pane_sender:
                return wake_failure(
                    session_id,
                    method="tmux_pane",
                    error_code="no_live_wake_channel",
                    error_message="No tmux pane sender is configured",
                )
            if not self._should_send_live_wake(session_id, session):
                return wake_debounced_result(session_id, method="tmux_pane")
            tmux_socket_path = terminal_route.tmux_socket_path
            current, state_failure = await self._preflight_live_side_effect(session_id)
            if state_failure is not None:
                return state_failure
            if current is not None:
                session = current
            blocked = await self._composer_blocks_wake(
                session_id, session, None, method="tmux_pane", priority=priority
            )
            if blocked is not None:
                return blocked
            try:
                await self._tmux_pane_sender(
                    tmux_pane,
                    CONTINUE_WAKE_MESSAGE,
                    tmux_socket_path,
                    submit=True,
                    clear_before_submit=True,
                    cli_source=getattr(session, "source", None),
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
                return wake_failure(
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
                return wake_failure(
                    session_id,
                    method="tmux_pane",
                    error_code="tmux_pane_wake_failed",
                    error_message=detail,
                )

        # Terminal agent → try tmux, then SDK. Both are wake signals only.
        if not self._should_send_live_wake(session_id, session):
            return wake_debounced_result(session_id, method="live_wake")

        wake_identity = terminal_route.tmux_session
        if wake_identity and self._tmux_sender:
            current, state_failure = await self._preflight_live_side_effect(session_id)
            if state_failure is not None:
                return state_failure
            if current is not None:
                session = current
            blocked = await self._composer_blocks_wake(
                session_id, session, None, method="tmux", priority=priority
            )
            if blocked is not None:
                return blocked
            try:
                await self._tmux_sender(
                    wake_identity,
                    CONTINUE_WAKE_MESSAGE,
                    submit=True,
                    clear_before_submit=True,
                    cli_source=getattr(session, "source", None),
                )
                self._record_live_wake(session_id, session)
                return {
                    "session_id": session_id,
                    "delivered": True,
                    "method": "tmux",
                }
            except Exception as exc:
                from gobby.terminals.runtime import (
                    AutomaticWriteDeclined,
                    IndeterminateWrite,
                )

                if isinstance(exc, IndeterminateWrite):
                    return {
                        "session_id": session_id,
                        "delivered": False,
                        "method": "tmux",
                        "indeterminate": True,
                        "error_message": exc.detail,
                    }
                if isinstance(exc, AutomaticWriteDeclined):
                    logger.debug(
                        "tmux wake declined for session %s (tmux=%s): %s, trying SDK resume",
                        session_id,
                        wake_identity,
                        exc.reason,
                    )
                else:
                    logger.warning(
                        "tmux wake failed for session %s (tmux=%s), trying SDK resume",
                        session_id,
                        wake_identity,
                        exc_info=True,
                    )

        tmux_pane = terminal_route.tmux_pane
        if tmux_pane and self._tmux_pane_sender:
            tmux_socket_path = terminal_route.tmux_socket_path
            current, state_failure = await self._preflight_live_side_effect(session_id)
            if state_failure is not None:
                return state_failure
            if current is not None:
                session = current
            blocked = await self._composer_blocks_wake(
                session_id, session, None, method="tmux_pane", priority=priority
            )
            if blocked is not None:
                return blocked
            try:
                await self._tmux_pane_sender(
                    tmux_pane,
                    CONTINUE_WAKE_MESSAGE,
                    tmux_socket_path,
                    submit=True,
                    clear_before_submit=True,
                    cli_source=getattr(session, "source", None),
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
                current, state_failure = await self._preflight_live_side_effect(session_id)
                if state_failure is not None:
                    return state_failure
                if current is not None:
                    session = current
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

        return wake_failure(
            session_id,
            method=None,
            error_code="no_live_wake_channel",
            error_message="No live wake channel is available for this session",
        )

    async def _terminal_route_for_session(self, session: Any) -> SessionTerminalRoute:
        """Resolve managed ownership before retaining raw tmux fallbacks."""
        manager = self._terminal_manager
        if manager is None:
            return resolve_session_terminal_route(session, None)

        def read_terminal() -> SessionTerminalRoute:
            return resolve_session_terminal_route(session, manager)

        try:
            return cast(SessionTerminalRoute, await self._run_db(read_terminal))
        except Exception:
            logger.warning(
                "live terminal lookup failed for session %s",
                getattr(session, "id", None),
                exc_info=True,
            )
            return resolve_session_terminal_route(session, None)

    async def _preflight_live_side_effect(
        self,
        session_id: str,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        """Re-read lifecycle state immediately before a wake side effect."""

        def read_session() -> Any | None:
            with self._session_manager.db.bounded_transaction():
                return self._session_manager.get(session_id)

        session = await self._run_db(read_session)
        if session is None:
            return None, wake_failure(
                session_id,
                method=None,
                error_code="session_not_found",
                error_message=f"Session {session_id} not found",
            )
        return session, wake_state_failure(session_id, getattr(session, "status", None))

    async def _composer_blocks_wake(
        self,
        session_id: str,
        session: Any,
        terminal: Any | None,
        *,
        method: str,
        priority: str,
    ) -> dict[str, Any] | None:
        """Withhold the drain when the composer positively shows an operator draft.

        Only a ``draft`` read blocks; ``empty``, ``unknown``, a missing probe and
        a probe error all fall through to the blind drain. An urgent wake always
        drains. No debounce record is written, so the next wake probes again.
        """
        if priority == "urgent" or self._composer_probe is None:
            return None
        try:
            read = await self._composer_probe(session, terminal)
        except Exception:
            logger.debug("composer probe failed for session %s", session_id, exc_info=True)
            return None
        if read.state != "draft":
            return None
        logger.debug(
            "wake for session %s deferred to the next turn: composer holds an operator draft",
            session_id,
        )
        return composer_occupied_result(session_id, method=method)

    async def _send_managed_terminal_wake(
        self,
        session_id: str,
        session: Any,
        terminal: Any,
        send: TmuxSender,
        *,
        priority: str = "normal",
    ) -> dict[str, Any]:
        """Wake a session through the terminal row that hosts it.

        `send` is the composition root's wake sender: it resolves the row by
        identity and writes through the write coordinator, so the runtime comes
        from Terminal.backend and native rows are driven as well as tmux ones.
        """
        from gobby.terminals.runtime import AutomaticWriteDeclined, IndeterminateWrite

        terminal_id = str(terminal.id)
        current, state_failure = await self._preflight_live_side_effect(session_id)
        if state_failure is not None:
            return state_failure
        if current is not None:
            session = current
        blocked = await self._composer_blocks_wake(
            session_id, session, terminal, method="terminal", priority=priority
        )
        if blocked is not None:
            return blocked
        try:
            await send(
                terminal_id,
                CONTINUE_WAKE_MESSAGE,
                submit=True,
                clear_before_submit=True,
                cli_source=getattr(session, "source", None),
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
        except AutomaticWriteDeclined as exc:
            # The coordinator refused before dispatch, so nothing is on screen.
            # A refusal is a designed outcome, not a failure worth a traceback.
            logger.debug(
                "terminal wake declined for session %s (terminal=%s): %s",
                session_id,
                terminal_id,
                exc.reason,
            )
            return wake_failure(
                session_id,
                method="terminal",
                error_code=exc.reason,
                error_message=str(exc),
            ) | {"decline_reason": exc.reason}
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            logger.warning(
                "terminal wake failed for session %s (terminal=%s)",
                session_id,
                terminal_id,
                exc_info=True,
            )
            return wake_failure(
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

    async def _dispatch_web_chat_wake(self, session_id: str) -> dict[str, Any]:
        if self._web_chat_session_registry is None:
            return self._web_chat_no_live_result(session_id)

        _session, state_failure = await self._preflight_live_side_effect(session_id)
        if state_failure is not None:
            return state_failure

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
