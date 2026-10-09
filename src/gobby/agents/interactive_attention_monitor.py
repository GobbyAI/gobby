"""Monitor interactive terminal snapshots for prompts and provider stalls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from gobby.agents.detection.provider import DetectionRegistry
from gobby.agents.prompt_detector import PromptDetector, PromptKind
from gobby.agents.stall_classifier import StallClassifier, StallStatus
from gobby.storage.attention import session_attention_entry_id
from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER
from gobby.terminals.composer_attention import composer_attention_holds
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.write_coordinator import WriteCoordinator
from gobby.utils.logging import ThrottledLogger
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.attention import AttentionKind, AttentionStateManager
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager
    from gobby.terminals.runtime import TerminalRuntimeRegistry

logger = logging.getLogger(__name__)

_AGENT_RUN_PAGE_SIZE = 100
_INTERACTIVE_SESSION_PAGE_SIZE = 100
_pool_outage_log = ThrottledLogger()
_host_outage_log = ThrottledLogger()


class InteractiveAttentionMonitor:
    """Background poller for interactive terminal attention."""

    def __init__(
        self,
        detection_registry: DetectionRegistry,
        poll_interval: float = 5.0,
        session_manager: SessionManager | None = None,
        attention_manager: AttentionStateManager | None = None,
        prompt_detector: PromptDetector | None = None,
        stall_classifier: StallClassifier | None = None,
        *,
        registry: TerminalRuntimeRegistry,
        startup_ready: Callable[[], bool] | None = None,
        write_coordinator: WriteCoordinator | None = None,
        max_reprompt_attempts: int | None = None,
    ) -> None:
        self._poll_interval = poll_interval
        self._session_manager = session_manager
        self._attention_manager = attention_manager
        self._detection_registry = detection_registry
        self._prompt_detector = prompt_detector or PromptDetector(detection_registry)
        self._stall_classifier = stall_classifier or StallClassifier(detection_registry)
        # Resolve the runtime per terminal row so native and external terminals
        # use their own snapshot implementations.
        self._registry = registry
        self._startup_ready = startup_ready
        from gobby.agents.watchdog.interactive_capacity import InteractiveCapacityRecovery
        from gobby.config.tmux import TmuxConfig

        self._capacity_recovery = (
            InteractiveCapacityRecovery(
                session_manager,
                attention_manager,
                write_coordinator,
                max_reprompt_attempts
                if max_reprompt_attempts is not None
                else TmuxConfig().max_reprompt_attempts,
            )
            if session_manager is not None and attention_manager is not None
            else None
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def detection_registry(self) -> DetectionRegistry:
        return self._detection_registry

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop(), name="interactive-attention-monitor")
        logger.info("InteractiveAttentionMonitor started (interval=%.1fs)", self._poll_interval)

    async def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("InteractiveAttentionMonitor stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Infinite loop: sleep, check panes, repeat."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._check_attention()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("InteractiveAttentionMonitor poll error (continuing)")

    async def _check_attention(self) -> None:
        """Refresh interactive attention from live terminal snapshots."""
        if self._startup_ready is not None and not self._startup_ready():
            return
        if self._session_manager is None:
            return
        from gobby.storage.agents import LocalAgentRunManager

        try:
            arm = LocalAgentRunManager(self._session_manager.db)
            active_runs = await self._list_active_runs(arm)
        except Exception as exc:
            if is_pool_unavailable(exc):
                _pool_outage_log(
                    logger,
                    logging.WARNING,
                    "InteractiveAttentionMonitor: hub temporarily unavailable; skipping pass",
                )
            else:
                logger.warning(
                    "InteractiveAttentionMonitor: failed to list active agent runs",
                    exc_info=True,
                )
            return
        await self._check_attention_panes(active_runs=active_runs)

    async def _check_attention_panes(self, *, active_runs: Sequence[AgentRun]) -> None:
        """Report attention and recover confirmed capacity failures in placed seats."""
        manager = self._attention_manager
        session_manager = self._session_manager
        if manager is None or session_manager is None:
            return

        try:
            sessions = await self._list_interactive_sessions()
        except Exception:
            logger.warning(
                "InteractiveAttentionMonitor: failed to list interactive sessions",
                exc_info=True,
            )
            return

        active_agent_sessions = {
            run.child_session_id for run in active_runs if run.child_session_id is not None
        }
        active_interactive_ids = {session.id for session in sessions}
        if self._capacity_recovery is not None:
            self._capacity_recovery.prune(active_interactive_ids - active_agent_sessions)
        for attention in await asyncio.to_thread(manager.list_blocked):
            if (
                attention.run_id is None
                and attention.session_id is not None
                and attention.session_id not in active_interactive_ids
            ):
                await manager.transition_async(
                    asyncio.to_thread,
                    attention.entry_id,
                    state=None,
                    expected_attention_id=attention.attention_id,
                    expected_fingerprint=attention.fingerprint,
                )

        native_unavailable = False
        for session in sessions:
            if session.id in active_agent_sessions:
                continue
            row = None
            try:
                from gobby.storage.terminals import TerminalManager

                if self._session_manager is None:
                    await self._clear_attention_if_current(session_attention_entry_id(session.id))
                    continue
                row = await asyncio.to_thread(
                    TerminalManager(self._session_manager.db).get_live_for_session,
                    session.id,
                )
                if row is None:
                    await self._clear_attention_if_current(session_attention_entry_id(session.id))
                    continue
                if row.backend == "native" and native_unavailable:
                    continue
                snapshot = await self._registry.resolve(row.backend).snapshot(row, 15)
                pane_output = snapshot.text
                if (
                    pane_output is not None
                    and self._capacity_recovery is not None
                    and await self._capacity_recovery.check(session, row, pane_output)
                ):
                    continue
            except TimeoutError as exc:
                logger.debug(
                    "InteractiveAttentionMonitor: interactive terminal capture timed out",
                    extra={
                        "terminal_id": row.id if row is not None else "",
                        "session_id": session.id,
                        "provider": session.source or "",
                        "error": str(exc),
                    },
                )
                continue
            except HostUnavailableError:
                if row is not None and row.backend == "native":
                    native_unavailable = True
                    _host_outage_log(
                        logger,
                        logging.DEBUG,
                        "InteractiveAttentionMonitor: native host unavailable; retrying next pass",
                    )
                    continue
                logger.warning(
                    "InteractiveAttentionMonitor: failed to capture terminal for session %s",
                    session.id,
                    exc_info=True,
                )
                continue
            except Exception:
                logger.warning(
                    "InteractiveAttentionMonitor: failed to capture terminal for session %s",
                    session.id,
                    exc_info=True,
                )
                continue
            if pane_output is None:
                continue
            await self._sync_interactive_attention(
                session.id,
                session.source or "",
                pane_output,
                terminal_id=str(row.id),
            )

    async def _sync_interactive_attention(
        self,
        session_id: str,
        provider: str,
        pane_output: str,
        *,
        terminal_id: str | None = None,
    ) -> None:
        manager = self._attention_manager
        if manager is None:
            return
        prompt_detector = self._prompt_detector.for_provider(provider)
        stall_classifier = self._stall_classifier.for_provider(provider)
        reason: PromptKind | None = None
        kind: AttentionKind | None = None
        classification_reason: str | None = None
        detected = prompt_detector.detect_prompt(pane_output)
        if detected is not None:
            reason = detected.kind
            kind = "actionable"
        else:
            classification = stall_classifier.classify(session_id, pane_output=pane_output)
            if classification.status is StallStatus.PROVIDER_STALL:
                reason = "stall"
                kind = "non_actionable"
                classification_reason = classification.reason

        if reason is None or kind is None:
            if (
                self._capacity_recovery is not None
                and await self._capacity_recovery.has_current_failure(session_id)
            ):
                return
            await self._resolve_interactive_lifecycle_wait(session_id)
            await self._clear_attention_if_current(
                session_attention_entry_id(session_id), terminal_id=terminal_id
            )
            return
        prompt_payload = (
            detected
            if detected is not None and detected.kind == reason
            else prompt_detector.classification_payload(
                kind=reason,
                label=classification_reason or reason,
            )
        )
        if reason in {"approval", "question"}:
            await self._enter_interactive_lifecycle_wait(
                session_id,
                provider,
                reason,
                prompt_payload.fingerprint,
            )
        await manager.transition_async(
            asyncio.to_thread,
            session_attention_entry_id(session_id),
            state="blocked",
            session_id=session_id,
            reason=reason,
            kind=kind,
            fingerprint=prompt_payload.fingerprint,
            payload=prompt_payload.to_payload(),
        )

    async def _enter_interactive_lifecycle_wait(
        self,
        session_id: str,
        provider: str,
        reason: PromptKind,
        fingerprint: str,
    ) -> None:
        session_manager = self._session_manager
        if session_manager is None:
            return

        def enter() -> None:
            from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer

            lifecycle = TurnLifecycleReducer(session_manager)
            current = lifecycle.get(session_id)
            lifecycle.enter_wait(
                session_id,
                kind="approval" if reason == "approval" else "input",
                token=fingerprint,
                evidence=TurnEvidence(source=f"{provider}.pane", generation=current.generation),
            )

        await asyncio.to_thread(enter)

    async def _resolve_interactive_lifecycle_wait(self, session_id: str) -> None:
        manager = self._attention_manager
        session_manager = self._session_manager
        if manager is None or session_manager is None:
            return
        current_attention = await asyncio.to_thread(
            manager.get,
            session_attention_entry_id(session_id),
        )
        fingerprint = current_attention.fingerprint if current_attention is not None else None
        if (
            current_attention is None
            or current_attention.reason not in {"approval", "question"}
            or fingerprint is None
        ):
            return

        def resolve() -> None:
            from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer

            lifecycle = TurnLifecycleReducer(session_manager)
            state = lifecycle.get(session_id)
            lifecycle.resolve_wait(
                session_id,
                token=fingerprint,
                resolution="ambiguous",
                evidence=TurnEvidence(source="pane.resolved", generation=state.generation),
            )

        await asyncio.to_thread(resolve)

    async def _clear_attention_if_current(
        self, entry_id: str, *, terminal_id: str | None = None
    ) -> None:
        """Clear the entry, except a withheld wake's item while its composer still blocks."""
        manager = self._attention_manager
        if manager is None:
            return
        current = await asyncio.to_thread(manager.get, entry_id)
        if current is None or current.state is None:
            return
        if terminal_id is not None and composer_attention_holds(current, terminal_id):
            return
        await manager.transition_async(
            asyncio.to_thread,
            entry_id,
            state=None,
            expected_attention_id=current.attention_id,
            expected_fingerprint=current.fingerprint,
        )

    async def _list_active_runs(
        self,
        manager: LocalAgentRunManager,
    ) -> list[AgentRun]:
        runs: list[AgentRun] = []
        offset = 0
        while True:
            page = await asyncio.to_thread(
                manager.list_active_for_machine,
                require_machine_id(),
                limit=_AGENT_RUN_PAGE_SIZE,
                offset=offset,
            )
            runs.extend(page)
            if len(page) < _AGENT_RUN_PAGE_SIZE:
                return runs
            offset += len(page)

    async def _list_interactive_sessions(self) -> list[Session]:
        session_manager = self._session_manager
        if session_manager is None:
            return []
        sessions: list[Session] = []
        cursor_updated_at: str | None = None
        cursor_id: str | None = None
        while True:
            page = await asyncio.to_thread(
                session_manager.list,
                statuses=list(LIVE_SESSION_STATUS_ORDER),
                modes=["interactive"],
                limit=_INTERACTIVE_SESSION_PAGE_SIZE,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
            )
            sessions.extend(page)
            if len(page) < _INTERACTIVE_SESSION_PAGE_SIZE:
                return sessions
            cursor_updated_at = page[-1].updated_at.isoformat()
            cursor_id = page[-1].id
