"""Wake dispatcher for notifying sessions when async operations complete.

Routes wake messages based on session type after first persisting a durable
InterSessionMessage:
- Any session Gobby owns a live terminal row for: managed terminal wake through
  that row, whatever its backend (tmux or native)
- SDK agents (agent_depth > 0, sdk_session_id): SDK resume wake signal
- Terminal sessions without a live row: explicit missing-channel result
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.events.live_wake import (
    RETRYABLE_WAKE_SKIPS,
    ActivityProbe,
    composer_occupied_result,
    composer_unconfirmed_result,
    handoff_delivery_skip,
    normalize_live_wake_result,
    wake_debounced_result,
    wake_failure,
    wake_state_failure,
)
from gobby.events.wake_active_recovery import (
    reconcile_idle_prompt_session,
    reconcile_restart_stale_session,
    reconcile_restart_stale_sessions,
)
from gobby.events.wake_notifications import persist_completion_notification
from gobby.events.wake_terminal_resolution import (
    LiveTerminalResolver,
    SessionTerminalRoute,
    resolve_session_terminal_route,
)
from gobby.terminals.composer_lock import composer_action_lock
from gobby.utils.datetime import utc_now
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

CONTINUE_WAKE_MESSAGE = "[Gobby] Check messages"
CONTINUE_WAKE_SIGNAL = f"{CONTINUE_WAKE_MESSAGE}\n"

# Session variable holding when the last delivered live wake was attempted. The
# wake stays outstanding, and later wakes of any priority are skipped, while it
# is fresh, mail sent by then is unread, and nothing has been read since (#23125).
LIVE_WAKE_SENT_AT_VARIABLE = "live_wake_sent_at"
# A wake reported delivered but lost (e.g. dropped by the TUI) expires after
# this, so the next message wakes normally.
LIVE_WAKE_FRESH_SECONDS = 30.0
# Bounds SDK-resume and web-chat wakes only, which have no internal timeout.
# Never wrap the tmux senders in wait_for: every tmux subprocess is already
# bounded (TmuxTextInjectionTimeout), and an outer cancellation can land
# between paste-buffer and Enter, leaving pasted text unsubmitted.
LIVE_WAKE_TIMEOUT_SECONDS = 5.0

# Bounded exponential backoff for a wake withheld from a composer that is not
# confirmed empty. The message stays durable, so retrying only shortens the
# delay before the session sees it; giving up never loses it.
COMPOSER_RETRY_BASE_SECONDS = 15.0
COMPOSER_RETRY_MAX_SECONDS = 240.0

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
        composer_confirmed_empty: bool = False,
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
    # False once a probe confirmed the composer empty under the composer lock: the
    # drain could then only delete keystrokes an operator typed after that read.
    drain: bool = True


class NativeBatchSender(Protocol):
    def __call__(
        self, targets: list[NativeWakeTarget]
    ) -> Coroutine[Any, Any, list[dict[str, Any]]]: ...


class WakeDispatcher:
    """Dispatches wake messages to sessions based on their type.

    Constructor args:
        session_manager: For looking up session metadata (agent_depth, terminal_context)
        ism_manager: For creating InterSessionMessages (durable fallback)
        tmux_sender: Optional async callable to send keys to a managed terminal ID
        sdk_resumer: Optional async callable to resume an SDK session with a new prompt
        agent_run_manager: Optional manager for looking up sdk_session_id from agent runs
        terminal_manager: Optional lookup for the live terminal row hosting a session
    """

    def __init__(
        self,
        session_manager: SessionManager,
        ism_manager: InterSessionMessageManager,
        tmux_sender: TmuxSender | None = None,
        native_batch_sender: NativeBatchSender | None = None,
        sdk_resumer: SdkResumer | None = None,
        agent_run_manager: LocalAgentRunManager | None = None,
        web_chat_session_registry: WebChatSessionRegistryProtocol | None = None,
        terminal_manager: LiveTerminalResolver | None = None,
        run_db: RunDb | None = None,
        lifecycle_refresh: LifecycleRefresh | None = None,
        activity_probe: ActivityProbe | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._ism_manager = ism_manager
        self._tmux_sender = tmux_sender
        self._native_batch_sender = native_batch_sender
        self._sdk_resumer = sdk_resumer
        self._agent_run_manager = agent_run_manager
        self._web_chat_session_registry = web_chat_session_registry
        self._terminal_manager = terminal_manager
        self._run_db = run_db or _default_run_db
        self._lifecycle_refresh = lifecycle_refresh
        self._activity_probe = activity_probe
        self._restart_horizon_ms: int | None = None
        self._restart_excluded_session_ids: frozenset[str] = frozenset()
        self._live_wake_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._deferred_refreshes: dict[str, asyncio.Task[None]] = {}
        self._composer_retries: dict[str, asyncio.Task[None]] = {}
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    async def reconcile_restart_active_sessions(
        self,
        *,
        restart_horizon_ms: int | None,
        excluded_session_ids: frozenset[str],
        recovery_safe: bool,
    ) -> tuple[str, ...]:
        """Run initial reconciliation, then enable the same policy for later wakes."""
        paused = await reconcile_restart_stale_sessions(
            session_manager=self._session_manager,
            terminal_manager=self._terminal_manager,
            activity_probe=self._activity_probe,
            run_db=self._run_db,
            restart_horizon_ms=restart_horizon_ms,
            excluded_session_ids=excluded_session_ids,
            recovery_safe=recovery_safe,
        )
        if recovery_safe and isinstance(restart_horizon_ms, int):
            self._restart_horizon_ms = restart_horizon_ms
            self._restart_excluded_session_ids = excluded_session_ids
        return paused

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
        *,
        bypass_debounce: bool = False,
        prompt: str = CONTINUE_WAKE_MESSAGE,
    ) -> dict[str, Any]:
        """Persist a notification, then wake the session.

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
        live_result = await self.dispatch_live_wake(
            session_id, priority=priority, bypass_debounce=bypass_debounce, prompt=prompt
        )
        return {**live_result, "ism_persisted": True}

    async def dispatch_live_wake(
        self,
        session_id: str,
        *,
        priority: str = "normal",
        bypass_debounce: bool = False,
        prompt: str = CONTINUE_WAKE_MESSAGE,
    ) -> dict[str, Any]:
        """Send a live wake signal after durable mailbox storage is complete."""
        self._require_owner_loop()
        lock = self._live_wake_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._live_wake_locks[session_id] = lock
        async with lock:
            result = await self._dispatch_live_wake_unlocked(
                session_id, priority=priority, bypass_debounce=bypass_debounce, prompt=prompt
            )
            self._schedule_wake_followup(session_id, result, priority=priority)
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
        for session_id, result in zip(session_ids, results, strict=True):
            self._schedule_wake_followup(session_id, result, priority=priority)
        return [normalize_live_wake_result(result) for result in results]

    def _schedule_wake_followup(
        self,
        session_id: str,
        result: dict[str, Any],
        *,
        priority: str,
    ) -> None:
        """Keep a durable wake moving after it was withheld instead of delivered.

        Every path that withholds a wake owes the message a next attempt: an
        active row is refreshed for a later dispatch, and a composer that was not
        confirmed empty is retried with backoff. A deferred refresh that ends in
        one of those skips is itself such a path, so it calls back here rather
        than dropping the withheld message after its single retry.
        """
        skipped = result.get("skipped")
        if skipped == "session_active":
            self._schedule_deferred_refresh(session_id, priority=priority)
        elif skipped in RETRYABLE_WAKE_SKIPS:
            self._schedule_composer_retry(session_id, priority=priority)

    def _schedule_deferred_refresh(self, session_id: str, *, priority: str) -> None:
        """Refresh stale active state after returning the durable wake outcome."""
        if self._lifecycle_refresh is None or session_id in self._deferred_refreshes:
            return
        task = asyncio.create_task(self._refresh_and_retry_wake(session_id, priority=priority))
        self._deferred_refreshes[session_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._deferred_refreshes.get(session_id) is completed:
                self._deferred_refreshes.pop(session_id, None)

        task.add_done_callback(forget)

    def _schedule_composer_retry(self, session_id: str, *, priority: str) -> None:
        """Retry a wake withheld from a composer that was not confirmed empty.

        Each withheld attempt doubles the wait until ``COMPOSER_RETRY_MAX_SECONDS``.
        Keep one retry task until a confirmed empty composer accepts the wake or
        its lifecycle/terminal outcome no longer permits delivery.
        """
        if session_id in self._composer_retries:
            return
        task = asyncio.create_task(self._retry_withheld_wake(session_id, priority=priority))
        self._composer_retries[session_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._composer_retries.get(session_id) is completed:
                self._composer_retries.pop(session_id, None)

        task.add_done_callback(forget)

    async def _retry_withheld_wake(self, session_id: str, *, priority: str) -> None:
        delay = COMPOSER_RETRY_BASE_SECONDS
        while True:
            await self._composer_retry_wait(delay)
            lock = self._live_wake_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._live_wake_locks[session_id] = lock
            try:
                async with lock:
                    result = await self._dispatch_live_wake_unlocked(
                        session_id, priority=priority, bypass_debounce=True
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            skipped = result.get("skipped")
            if skipped not in RETRYABLE_WAKE_SKIPS:
                return
            if skipped == "session_active":
                # Active rows have their own lifecycle refresh path.
                self._schedule_deferred_refresh(session_id, priority=priority)
                return
            delay = min(delay * 2, COMPOSER_RETRY_MAX_SECONDS)

    async def _composer_retry_wait(self, delay: float) -> None:
        """Sleep between composer retries; tests replace this to avoid real time."""
        await asyncio.sleep(delay)

    async def _pause_idle_prompt(self, session_id: str) -> str:
        """Drop active at an idle empty prompt; return the reconcile outcome.

        ``lifecycle_refresh`` only flushes the transcript. A provider turn can
        end on screen while the row stays active, and the retry would then
        decline ``session_active`` again. Two idle reads plus an exact
        compare-and-set pause the row before that retry.
        """
        probe = self._activity_probe
        if probe is None:
            return "no_probe"
        observed = await self._run_db(self._session_manager.get, session_id)
        if observed is None:
            return "no_session"
        route = await self._terminal_route_for_session(observed)
        return await reconcile_idle_prompt_session(
            session_manager=self._session_manager,
            observed=observed,
            terminal=route.managed_terminal,
            activity_probe=probe,
            run_db=self._run_db,
        )

    async def _refresh_and_retry_wake(self, session_id: str, *, priority: str) -> None:
        assert self._lifecycle_refresh is not None
        # Phase marks: refresh, idle-prompt reconcile, lock wait, dispatch. The
        # four phases tile the span, so their sum is duration_ms.
        started = time.monotonic()
        try:
            await self._lifecycle_refresh(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Lifecycle refresh failed before retrying wake for session %s",
                session_id,
                exc_info=True,
            )
            return
        refreshed = time.monotonic()
        try:
            idle = await self._pause_idle_prompt(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            idle = "error"
            logger.warning(
                "Idle-prompt reconcile failed before retrying wake for session %s",
                session_id,
                exc_info=True,
            )
        reconciled = time.monotonic()
        lock = self._live_wake_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._live_wake_locks[session_id] = lock
        try:
            async with lock:
                locked = time.monotonic()
                result = await self._dispatch_live_wake_unlocked(session_id, priority=priority)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Deferred wake failed for session %s", session_id, exc_info=True)
            return
        finished = time.monotonic()
        skipped = result.get("skipped")
        # The refresh may have unpaused the row only to find a composer that was
        # not confirmed empty: the withheld message still owes a retry.
        self._schedule_wake_followup(session_id, result, priority=priority)
        # A debounced skip is routine; session_active stays at INFO because it is
        # the only trace of a session stranded as active (#22887).
        logger.log(
            logging.DEBUG if skipped == "debounced" else logging.INFO,
            "Deferred wake for session %s: delivered=%s method=%s skipped=%s idle=%s "
            "duration_ms=%.1f refresh_ms=%.1f reconcile_ms=%.1f lock_wait_ms=%.1f "
            "dispatch_ms=%.1f",
            session_id,
            result.get("delivered"),
            result.get("method"),
            skipped,
            idle,
            (finished - started) * 1000,
            (refreshed - started) * 1000,
            (reconciled - refreshed) * 1000,
            (locked - reconciled) * 1000,
            (finished - locked) * 1000,
        )

    async def _dispatch_live_wake_unlocked(
        self,
        session_id: str,
        *,
        session: Any | None = None,
        priority: str = "normal",
        bypass_debounce: bool = False,
        prompt: str = CONTINUE_WAKE_MESSAGE,
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
            if self._activity_probe is not None and self._restart_horizon_ms is not None:
                terminal_route = await self._terminal_route_for_session(session)
                reconciled = await reconcile_restart_stale_session(
                    session_manager=self._session_manager,
                    observed=session,
                    terminal=terminal_route.managed_terminal,
                    activity_probe=self._activity_probe,
                    run_db=self._run_db,
                    restart_horizon_ms=self._restart_horizon_ms,
                    excluded_session_ids=self._restart_excluded_session_ids,
                )
                if reconciled is not None:
                    session = reconciled
            if getattr(session, "status", None) == "active":
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
            if not bypass_debounce and not await self._should_send_live_wake(session_id):
                return wake_debounced_result(session_id, method="web_chat")
            attempted_at = utc_now()
            result = await self._dispatch_web_chat_wake(session_id, priority=priority)
            if result.get("delivered"):
                await self._record_live_wake(session_id, attempted_at)
            return result

        # The managed terminal row resolves the runtime for native and tmux
        # sessions. An unbound terminal has no safe input target.
        terminal_route = await self._terminal_route_for_session(session)
        terminal = terminal_route.managed_terminal
        if terminal is not None and self._tmux_sender is not None:
            if not bypass_debounce and not await self._should_send_live_wake(session_id):
                return wake_debounced_result(session_id, method="terminal")
            return await self._send_managed_terminal_wake(
                session_id,
                session,
                terminal,
                self._tmux_sender,
                priority=priority,
                prompt=prompt,
            )

        if agent_depth == 0:
            return wake_failure(
                session_id,
                method=None,
                error_code="no_live_wake_channel",
                error_message="Session has no live terminal binding for wake",
            )

        # Terminal agents with no managed row may still have an SDK resume route.
        if not bypass_debounce and not await self._should_send_live_wake(session_id):
            return wake_debounced_result(session_id, method="live_wake")

        # SDK agent → try resume via sdk_session_id
        if self._sdk_resumer:
            sdk_session_id = await self._resolve_sdk_session_id(session_id)
            if sdk_session_id:
                _, state_failure = await self._preflight_live_side_effect(
                    session_id, priority=priority
                )
                if state_failure is not None:
                    return state_failure
                attempted_at = utc_now()
                try:
                    await asyncio.wait_for(
                        self._sdk_resumer(sdk_session_id, f"{prompt}\n"),
                        timeout=LIVE_WAKE_TIMEOUT_SECONDS,
                    )
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
                await self._record_live_wake(session_id, attempted_at)
                return {
                    "session_id": session_id,
                    "delivered": True,
                    "method": "sdk",
                }

        return wake_failure(
            session_id,
            method=None,
            error_code="no_live_wake_channel",
            error_message="No live wake channel is available for this session",
        )

    async def _terminal_route_for_session(self, session: Any) -> SessionTerminalRoute:
        """Resolve the session's live managed terminal binding."""
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
        *,
        priority: str = "normal",
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
        status = getattr(session, "status", None)
        if status == "active" and priority != "urgent":
            return session, {
                "session_id": session_id,
                "delivered": False,
                "method": "next_call_context",
                "skipped": "session_active",
                "ism_persisted": True,
            }
        state_failure = wake_state_failure(session_id, status)
        if state_failure is not None:
            return session, state_failure

        def read_variables() -> dict[str, Any]:
            return SessionVariableManager(self._session_manager.db).get_variables(session_id)

        return session, handoff_delivery_skip(session_id, await self._run_db(read_variables))

    async def _composer_blocks_wake(
        self,
        session_id: str,
        session: Any,
        terminal: Any | None,
        *,
        method: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Withhold the drain unless a probe positively confirms an empty composer.

        Returns the withheld outcome (``None`` to deliver) and whether the composer
        was confirmed empty; only an unconfirmed delivery keeps the blind drain.

        Only an ``empty`` read authorizes typing. A ``draft`` blocks, and an
        ``unknown`` read blocks too when the provider can classify its composer
        at all, because the frame may hold a draft the probe could not read. A
        provider whose manifest has no composer rules answers ``unknown`` to
        every probe, so withholding there would starve it forever; that case
        keeps the pre-existing behavior. A missing probe has no safer read to
        offer, so it stays on its existing path; a probe *error* is different:
        the composer is unreadable, which is exactly the unconfirmed state, so
        it withholds and retries rather than blinding typing into a composer it
        could not read. Priority remains on the durable notification; it never
        authorizes typing over a draft. No debounce record is written, so the
        next wake probes again.

        A live turn fingerprint blocks even on an ``empty`` composer: the row was
        reconciled idle earlier, so a turn that started since then would be
        steered or cancelled by this write, and an earlier empty snapshot alone
        cannot authorize a later overlapping write.
        """
        if self._activity_probe is None:
            return None, False
        try:
            activity = await self._activity_probe(session, terminal)
        except Exception:
            logger.debug("activity probe failed for session %s", session_id, exc_info=True)
            return composer_unconfirmed_result(session_id, method=method), False
        if activity.turn_in_flight_fingerprint is not None:
            return composer_unconfirmed_result(session_id, method=method), False
        state = activity.composer.state
        if state == "empty":
            return None, True
        if state == "draft":
            excerpt = " ".join((activity.composer.line or "").split())
            if len(excerpt) > 160:
                excerpt = f"{excerpt[:157]}..."
            logger.warning(
                "wake for session %s deferred: composer holds an operator draft: %s",
                session_id,
                excerpt,
            )
            return composer_occupied_result(session_id, method=method), False
        if not activity.composer_probeable:
            return None, False
        return composer_unconfirmed_result(session_id, method=method), False

    async def _send_managed_terminal_wake(
        self,
        session_id: str,
        session: Any,
        terminal: Any,
        send: TmuxSender,
        *,
        priority: str = "normal",
        prompt: str = CONTINUE_WAKE_MESSAGE,
    ) -> dict[str, Any]:
        """Wake a session through the terminal row that hosts it.

        `send` is the composition root's wake sender: it resolves the row by
        identity and writes through the write coordinator, so the runtime comes
        from Terminal.backend and native rows are driven as well as tmux ones.
        """
        from gobby.terminals.runtime import AutomaticWriteDeclined, IndeterminateWrite

        terminal_id = str(terminal.id)
        # Hold the shared composer lock across the lifecycle preflight, the probe
        # and the write: the empty snapshot that authorizes typing cannot be
        # overtaken by a handoff staging text in the gap between them, and a row
        # that turned active while this wake waited for the lock is not steered.
        async with composer_action_lock(terminal_id):
            current, state_failure = await self._preflight_live_side_effect(
                session_id, priority=priority
            )
            if state_failure is not None:
                return state_failure
            if current is not None:
                session = current
            blocked, confirmed_empty = await self._composer_blocks_wake(
                session_id, session, terminal, method="terminal"
            )
            if blocked is not None:
                return blocked
            if confirmed_empty:
                # The sender needs the positive proof to settle an old wake latch;
                # merely opting out of a drain does not supply that proof.
                send = partial(send, composer_confirmed_empty=True)
            attempted_at = utc_now()
            try:
                await send(
                    terminal_id,
                    prompt,
                    submit=True,
                    clear_before_submit=not confirmed_empty,
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
        await self._record_live_wake(session_id, attempted_at)
        return {
            "session_id": session_id,
            "delivered": True,
            "method": "terminal",
        }

    async def _dispatch_web_chat_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        if self._web_chat_session_registry is None:
            return self._web_chat_no_live_result(session_id)

        _session, state_failure = await self._preflight_live_side_effect(
            session_id, priority=priority
        )
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

    async def _should_send_live_wake(self, session_id: str) -> bool:
        """Return False while the last delivered wake to this session is outstanding.

        A delivered wake is outstanding while it is fresh, mail sent by its
        attempt is unread, and no message has been read since the attempt; any
        read consumes it. Durable ISMs are stored unconditionally, so later
        messages still queue and the agent sees them on that read.
        """

        def wake_outstanding() -> bool:
            variables = SessionVariableManager(self._session_manager.db).get_variables(session_id)
            sent_at = variables.get(LIVE_WAKE_SENT_AT_VARIABLE)
            if not isinstance(sent_at, str):
                return False
            try:
                cutoff = datetime.fromisoformat(sent_at)
            except ValueError:
                return False
            if (utc_now() - cutoff).total_seconds() >= LIVE_WAKE_FRESH_SECONDS:
                return False
            return self._ism_manager.has_unread_without_read_since(session_id, cutoff)

        return not await self._run_db(wake_outstanding)

    async def _record_live_wake(self, session_id: str, attempted_at: datetime) -> None:
        """Record a delivered live wake by the time its attempt started.

        Messages sent after that time did not ride this wake, and a read after
        it consumes the wake, so the next message wakes the session again.
        """
        await self._run_db(
            SessionVariableManager(self._session_manager.db).set_variable,
            session_id,
            LIVE_WAKE_SENT_AT_VARIABLE,
            attempted_at.isoformat(),
        )

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
