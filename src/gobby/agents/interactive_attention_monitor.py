"""Monitor interactive terminal snapshots for prompts and provider stalls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gobby.agents.detection.provider import DetectionRegistry
from gobby.agents.prompt_detector import PromptDetector, PromptKind
from gobby.agents.provider_quota_monitor import ProviderQuotaMonitor
from gobby.agents.stall_classifier import StallClassifier, StallStatus
from gobby.hub.machine_routing import is_local_machine
from gobby.hub.remote_runtime import resolve_terminal_runtime
from gobby.providers.quota_signals import pane_generation
from gobby.sessions.transcript_cursor import ClaudeTranscriptCursor
from gobby.storage.attention import session_attention_entry_id
from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.storage.sessions import LIVE_SESSION_STATUS_ORDER
from gobby.terminals.composer_attention import composer_attention_holds
from gobby.terminals.composer_ledger import (
    composer_unconsumed_submit_at,
    record_composer_local_command,
)
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.write_coordinator import WriteCoordinator
from gobby.utils.logging import ThrottledLogger
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.providers.capacity_service import ProviderCapacityService
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.attention import AttentionKind, AttentionStateManager
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager
    from gobby.storage.terminals import Terminal
    from gobby.terminals.runtime import SnapshotResult, TerminalRuntimeRegistry

logger = logging.getLogger(__name__)

_AGENT_RUN_PAGE_SIZE = 100
_INTERACTIVE_SESSION_PAGE_SIZE = 100
_pool_outage_log = ThrottledLogger()
_host_outage_log = ThrottledLogger()


def _claude_local_command_at(transcript_path: str) -> datetime | None:
    return ClaudeTranscriptCursor.at_eof(transcript_path).latest_local_command_at()


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
        provider_capacity_service: ProviderCapacityService | None = None,
    ) -> None:
        self._quota_monitor = (
            None
            if provider_capacity_service is None
            else ProviderQuotaMonitor(provider_capacity_service, owner="interactive")
        )
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
            active_agent_sessions = await self._list_active_agent_sessions(arm)
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
        await self._check_attention_panes(active_agent_sessions=active_agent_sessions)

    async def _check_attention_panes(self, *, active_agent_sessions: set[str]) -> None:
        """Report attention and recover confirmed capacity failures in placed seats.

        Quota observation runs whether or not attention is enabled: a missing attention
        manager only removes the attention and recovery work from the same capture.
        """
        manager = self._attention_manager
        session_manager = self._session_manager
        quota = self._quota_monitor
        if session_manager is None or (manager is None and quota is None):
            return

        try:
            sessions = await self._list_interactive_sessions()
        except Exception:
            logger.warning(
                "InteractiveAttentionMonitor: failed to list interactive sessions",
                exc_info=True,
            )
            return

        active_interactive_ids = {session.id for session in sessions}
        if self._capacity_recovery is not None:
            self._capacity_recovery.prune(active_interactive_ids - active_agent_sessions)
        if manager is not None:
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
        live_generations: dict[str, set[str]] = {}
        lookups_complete = True
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
                if quota is not None and session.source:
                    # Every live placed terminal counts, including one whose capture fails.
                    live_generations.setdefault(session.source, set()).add(pane_generation(row))
                local_native = row.backend == "native" and is_local_machine(row.machine_id)
                if local_native and native_unavailable:
                    self._rebaseline_quota(row)
                    continue
                runtime = resolve_terminal_runtime(self._registry, row, self._session_manager.db)
                snapshot = await runtime.snapshot(row, 15)
                await self._observe_quota(session, row, snapshot)
                pane_output = snapshot.text
                if (
                    pane_output is not None
                    and self._capacity_recovery is not None
                    and await self._capacity_recovery.check(session, row, pane_output)
                ):
                    continue
            except TimeoutError as exc:
                lookups_complete = lookups_complete and row is not None
                self._rebaseline_quota(row)
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
                lookups_complete = lookups_complete and row is not None
                self._rebaseline_quota(row)
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
                lookups_complete = lookups_complete and row is not None
                self._rebaseline_quota(row)
                logger.warning(
                    "InteractiveAttentionMonitor: failed to capture terminal for session %s",
                    session.id,
                    exc_info=True,
                )
                continue
            if pane_output is None or manager is None:
                continue
            await self._settle_local_command(session, str(row.id))
            await self._sync_interactive_attention(
                session.id,
                session.source or "",
                pane_output,
                terminal_id=str(row.id),
            )
        if quota is not None and lookups_complete:
            # A failed lookup leaves liveness unknown, so this pass retires nothing.
            try:
                await quota.retire_dead(live_generations)
            except Exception:
                logger.warning("Failed to retire interactive pane quota generations", exc_info=True)

    def _rebaseline_quota(self, row: Terminal | None) -> None:
        """An outage or failed capture ends the terminal's baseline; the next capture starts fresh."""
        if self._quota_monitor is not None and row is not None:
            self._quota_monitor.rebaseline(row)

    async def _observe_quota(
        self, session: Session, row: Terminal, snapshot: SnapshotResult
    ) -> None:
        """Feed one owned capture to the shared sink; an observation failure gates nothing."""
        quota = self._quota_monitor
        if quota is None or not session.source:
            return
        if snapshot.text is None:
            quota.rebaseline(row)
            return
        try:
            await quota.observe_capture(
                provider=session.source,
                terminal=row,
                snapshot=snapshot,
                detected_at=datetime.now(UTC),
            )
        except Exception:
            logger.warning(
                "InteractiveAttentionMonitor: failed to observe pane quota hints for session %s",
                session.id,
                exc_info=True,
            )

    async def _settle_local_command(self, session: Session, terminal_id: str) -> None:
        """Settle a flagged human submit that Claude ran as a local slash command.

        Claude runs typed slash commands natively, so no provider hook records their
        submit. The transcript is read only while the ledger holds such a submit.
        """
        if session.source != "claude" or composer_unconsumed_submit_at(terminal_id) is None:
            return
        transcript_path = getattr(session, "transcript_path", None)
        if not transcript_path:
            return
        try:
            row_at = await asyncio.to_thread(_claude_local_command_at, transcript_path)
        except Exception:
            logger.warning(
                "InteractiveAttentionMonitor: failed to read local command rows for terminal %s",
                terminal_id,
                exc_info=True,
            )
            return
        if row_at is not None:
            record_composer_local_command(terminal_id, row_at=row_at)

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

    async def _list_active_agent_sessions(
        self,
        manager: LocalAgentRunManager,
    ) -> set[str]:
        session_ids: set[str] = set()
        offset = 0
        while True:
            page = await asyncio.to_thread(
                manager.list_active_child_session_ids_for_machine,
                require_machine_id(),
                limit=_AGENT_RUN_PAGE_SIZE,
                offset=offset,
            )
            session_ids.update(page)
            if len(page) < _AGENT_RUN_PAGE_SIZE:
                return session_ids
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
                machine_id=require_machine_id(),
                limit=_INTERACTIVE_SESSION_PAGE_SIZE,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
            )
            sessions.extend(page)
            if len(page) < _INTERACTIVE_SESSION_PAGE_SIZE:
                return sessions
            cursor_updated_at = page[-1].updated_at.isoformat()
            cursor_id = page[-1].id
