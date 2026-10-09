"""Watch capacity failures in placed interactive seats without an agent run."""

import asyncio
import logging
from dataclasses import dataclass

from gobby.agents.idle_detector import IdleDetector
from gobby.agents.watchdog.models import CapacityRecoveryState
from gobby.agents.watchdog.recovery import pane_has_capacity_message
from gobby.agents.watchdog.registry import WatchdogReaderRegistry
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal
from gobby.terminals.composer_ledger import composer_drain_keys, read_composer
from gobby.terminals.runtime import Delivered, TerminalWriteError
from gobby.terminals.write_coordinator import WriteCoordinator, WriteRequest

logger = logging.getLogger(__name__)


@dataclass
class RecoveryState:
    capacity: CapacityRecoveryState
    failed_deliveries: int = 0
    exhausted: bool = False


class InteractiveCapacityRecovery:
    def __init__(
        self,
        sessions: SessionManager,
        attention: AttentionStateManager,
        coordinator: WriteCoordinator | None,
        max_attempts: int,
    ) -> None:
        self._lifecycle = TurnLifecycleReducer(sessions, attention)
        self._attention = attention
        self._readers = WatchdogReaderRegistry()
        self._coordinator = coordinator
        self._max_attempts = max_attempts
        self._states: dict[str, RecoveryState] = {}

    def prune(self, session_ids: set[str]) -> None:
        self._states = {key: state for key, state in self._states.items() if key in session_ids}

    async def has_current_failure(self, session_id: str) -> bool:
        state = await asyncio.to_thread(self._lifecycle.get, session_id)
        failure = state.provider_error
        return (
            state.turn_state == "terminal"
            and failure is not None
            and failure.generation == state.generation
        )

    async def check(self, session: Session, terminal: Terminal, pane_output: str) -> bool:
        reader = self._readers.for_provider(session.source or "")
        if reader is None or not pane_has_capacity_message(pane_output, reader):
            return False
        if not session.transcript_path:
            return False
        snapshot = await reader.read(session.transcript_path)
        if not snapshot.has_conclusive_capacity_error:
            return False
        error = snapshot.provider_error_event
        if error is None:
            return False
        state = self._states.get(session.id)
        if state is None or state.capacity.transcript_path != session.transcript_path:
            state = RecoveryState(CapacityRecoveryState(session.transcript_path))
            self._states[session.id] = state
        capacity = state.capacity
        if (
            capacity.last_error_line_num is not None
            and snapshot.latest_model_output_line_num is not None
            and snapshot.latest_model_output_line_num > capacity.last_error_line_num
        ):
            capacity.successful_reprompts = 0
            state.failed_deliveries = 0
            state.exhausted = False
        if capacity.last_error_line_num == error.line_num and not state.exhausted:
            return True
        if (
            not state.exhausted
            and self._coordinator is not None
            and capacity.successful_reprompts < self._max_attempts
        ):
            # Recheck under the shared logical-action lock before altering a composer;
            # the reprompt drains held daemon text, never a human draft.
            async with self._coordinator.logical_action_lock(terminal.id):
                if read_composer(str(terminal.id)).state not in {"empty", "held"}:
                    await self._attention.transition_async(
                        asyncio.to_thread,
                        session_attention_entry_id(session.id),
                        state="blocked",
                        session_id=session.id,
                        reason="stall",
                        kind="non_actionable",
                        fingerprint=f"capacity:{error.line_num}",
                        payload={
                            "label": snapshot.provider_error_reason or "Provider at capacity",
                            "detail": "Capacity retry waiting for an empty composer.",
                        },
                    )
                    return True
                fresh = await reader.read(session.transcript_path)
                if not fresh.has_conclusive_capacity_error or fresh.provider_error_event != error:
                    await self._clear_capacity_attention(session.id, error.line_num)
                    return True
                delivered = await self._reprompt(terminal, session, error.line_num)
            if delivered:
                capacity.last_error_line_num = error.line_num
                capacity.successful_reprompts += 1
                state.failed_deliveries = 0
                await self._clear_capacity_attention(session.id, error.line_num)
                return True
            state.failed_deliveries += 1
            if state.failed_deliveries < self._max_attempts:
                return True
        state.exhausted = True
        capacity.last_error_line_num = error.line_num

        def fail() -> None:
            current = self._lifecycle.get(session.id)
            self._lifecycle.record_provider_failure(
                session.id,
                error_type="capacity",
                message=snapshot.provider_error_reason or "Provider at capacity",
                retryable=False,
                max_resumes=0,
                evidence=TurnEvidence(
                    source=f"{session.source}.pane", generation=current.generation
                ),
            )
            failure = self._lifecycle.get(session.id).provider_error
            if failure is not None:
                self._lifecycle.block_provider_failure(
                    session.id, generation=failure.generation, attempts=failure.attempts
                )

        await asyncio.to_thread(fail)
        return True

    async def _clear_capacity_attention(self, session_id: str, error_line: int) -> None:
        entry_id = session_attention_entry_id(session_id)
        blocked = await asyncio.to_thread(self._attention.get, entry_id)
        if blocked is not None and blocked.fingerprint == f"capacity:{error_line}":
            await self._attention.transition_async(
                asyncio.to_thread,
                entry_id,
                state=None,
                expected_attention_id=blocked.attention_id,
                expected_fingerprint=blocked.fingerprint,
            )

    async def _reprompt(self, terminal: Terminal, session: Session, error_line: int) -> bool:
        coordinator = self._coordinator
        if coordinator is None:
            return False
        action = f"interactive-capacity:{session.id}:{error_line}"
        steps = [
            WriteRequest(
                terminal_id=terminal.id,
                action_key=action,
                origin="automatic",
                kind="key",
                payload=key,
            )
            for key in composer_drain_keys(str(terminal.id), session.source)
        ]
        steps.extend(
            [
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=action,
                    origin="automatic",
                    kind="text",
                    payload=IdleDetector.INTERACTIVE_CAPACITY_REPROMPT_MESSAGE,
                ),
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=action,
                    origin="automatic",
                    kind="key",
                    payload="enter",
                ),
            ]
        )
        try:
            outcome = await coordinator.run_sequence(
                terminal.id, action_key=action, origin="automatic", steps=steps
            )
        except TerminalWriteError:
            logger.warning(
                "Capacity retry delivery failed for session %s", session.id, exc_info=True
            )
            return False
        return isinstance(outcome, Delivered)
