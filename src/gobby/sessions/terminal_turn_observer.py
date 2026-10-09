"""Confirm mediated terminal interruption evidence for hook-limited providers."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal, Protocol

from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer

WriteOutcome = Literal["delivered", "indeterminate", "refused"]

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_AGY_INTERRUPT_RE = re.compile(
    r"\bInterrupted\s*[·:-]\s*What should Antigravity CLI do instead\?",
    re.IGNORECASE,
)
_SUPPORTED_PROVIDERS = frozenset({"agy"})
_INTERRUPT_INPUTS = frozenset({"\x03", "\x1b"})


async def _await_worker_before_cancellation(worker: Coroutine[Any, Any, bool]) -> bool:
    """Keep the caller's ordering lock until its thread finishes, even if cancelled."""
    task = asyncio.create_task(worker)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class _SessionStore(Protocol):
    def get(self, session_id: str) -> Any | None: ...


class _TerminalStore(Protocol):
    db: Any

    def get(self, terminal_id: str) -> Any | None: ...


@dataclass
class InterruptCandidate:
    terminal_id: str
    session_id: str
    provider: str
    generation: int
    provider_turn_key: str | None
    input_seq: int | None
    created_at: float
    output: str = ""


def is_interrupt_input(payload: str) -> bool:
    """Return whether one admitted keyboard write is exactly Esc or Ctrl-C."""
    return payload in _INTERRUPT_INPUTS


def _visible_output(value: str) -> str:
    return _CONTROL_RE.sub("", _ANSI_ESCAPE_RE.sub("", value))


class TerminalTurnObserver:
    """Correlate Gobby-mediated interrupt keys with current provider output.

    A key is only a candidate: AGY uses the same keys for ordinary UI
    dismissal. The current turn becomes interrupted only when matching provider
    output arrives before the candidate expires and before its lifecycle generation
    changes.
    """

    def __init__(
        self,
        sessions: _SessionStore,
        lifecycle: TurnLifecycleReducer,
        *,
        terminal_manager: _TerminalStore | None = None,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._sessions = sessions
        self._lifecycle = lifecycle
        self._terminal_manager = terminal_manager
        self._timeout_seconds = timeout_seconds
        self._clock = clock or time.monotonic
        self._candidates: dict[str, InterruptCandidate] = {}
        # Worker threads protect mutations; async callers preserve input/output order.
        self._lock = RLock()
        self._async_lock = asyncio.Lock()

    def set_terminal_manager(self, terminal_manager: _TerminalStore | None) -> None:
        with self._lock:
            self._terminal_manager = terminal_manager

    async def record_mediated_input_async(
        self,
        terminal_id: str,
        payload: str,
        outcome: WriteOutcome,
        *,
        input_seq: int | None = None,
    ) -> bool:
        async with self._async_lock:
            if outcome == "refused" or not is_interrupt_input(payload):
                return self.record_mediated_input(
                    terminal_id, payload, outcome, input_seq=input_seq
                )
            return await _await_worker_before_cancellation(
                asyncio.to_thread(
                    self.record_mediated_input,
                    terminal_id,
                    payload,
                    outcome,
                    input_seq=input_seq,
                )
            )

    async def observe_output_async(self, terminal_id: str, output: str) -> bool:
        async with self._async_lock:
            with self._lock:
                if terminal_id not in self._candidates:
                    return False
            return await _await_worker_before_cancellation(
                asyncio.to_thread(self.observe_output, terminal_id, output)
            )

    async def observe_run_output_async(self, run_id: str, output: str) -> bool:
        async with self._async_lock:
            with self._lock:
                if not self._candidates:
                    return False
            return await _await_worker_before_cancellation(
                asyncio.to_thread(self.observe_run_output, run_id, output)
            )

    def record_mediated_input(
        self,
        terminal_id: str,
        payload: str,
        outcome: WriteOutcome,
        *,
        input_seq: int | None = None,
    ) -> bool:
        """Record a delivered/indeterminate Esc or Ctrl-C for the current turn."""
        with self._lock:
            return self._record_mediated_input(terminal_id, payload, outcome, input_seq=input_seq)

    def _record_mediated_input(
        self,
        terminal_id: str,
        payload: str,
        outcome: WriteOutcome,
        *,
        input_seq: int | None,
    ) -> bool:
        self._expire()
        if outcome == "refused":
            return False
        if not is_interrupt_input(payload):
            self._candidates.pop(terminal_id, None)
            return False
        terminal = self._terminal_manager.get(terminal_id) if self._terminal_manager else None
        session_id = getattr(terminal, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            return False
        session = self._sessions.get(session_id)
        provider = str(getattr(session, "source", "")).lower()
        if provider not in _SUPPORTED_PROVIDERS:
            return False
        lifecycle = self._lifecycle.get(session_id)
        self._candidates[terminal_id] = InterruptCandidate(
            terminal_id=terminal_id,
            session_id=session_id,
            provider=provider,
            generation=lifecycle.generation,
            provider_turn_key=lifecycle.provider_turn_key,
            input_seq=input_seq,
            created_at=self._clock(),
        )
        return True

    def observe_output(self, terminal_id: str, output: str) -> bool:
        """Consume provider output and apply interruption on exact correlation."""
        with self._lock:
            return self._observe_output(terminal_id, output)

    def _observe_output(self, terminal_id: str, output: str) -> bool:
        self._expire()
        candidate = self._candidates.get(terminal_id)
        if candidate is None or not output:
            return False
        lifecycle = self._lifecycle.get(candidate.session_id)
        if lifecycle.generation != candidate.generation:
            self._candidates.pop(terminal_id, None)
            return False
        session = self._sessions.get(candidate.session_id)
        if session is None or getattr(session, "status", None) != "active":
            self._candidates.pop(terminal_id, None)
            return False
        candidate.output = (candidate.output + output)[-4096:]
        visible = _visible_output(candidate.output)
        if not _AGY_INTERRUPT_RE.search(visible):
            return False
        self._candidates.pop(terminal_id, None)
        result = self._lifecycle.end_turn(
            candidate.session_id,
            "user_interrupted",
            TurnEvidence(
                source="mediated_terminal",
                generation=candidate.generation,
                provider_turn_key=candidate.provider_turn_key,
                cursor=candidate.input_seq,
            ),
        )
        return result.applied

    def observe_run_output(self, run_id: str, output: str) -> bool:
        """Resolve agent-reader output to its live terminal, when available."""
        with self._lock:
            return self._observe_run_output(run_id, output)

    def _observe_run_output(self, run_id: str, output: str) -> bool:
        manager = self._terminal_manager
        if manager is None:
            return False
        row = manager.db.fetchone(
            """
            SELECT id FROM terminals
            WHERE agent_run_id = %s AND state IN ('pending', 'live')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (run_id,),
        )
        if row is None:
            return False
        terminal_id = row.get("id") if hasattr(row, "get") else row["id"]
        return self.observe_output(str(terminal_id), output)

    def clear_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._candidates.pop(terminal_id, None)

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            for terminal_id, candidate in tuple(self._candidates.items()):
                if candidate.session_id == session_id:
                    self._candidates.pop(terminal_id, None)

    def _expire(self) -> None:
        threshold = self._clock() - self._timeout_seconds
        for terminal_id, candidate in tuple(self._candidates.items()):
            if candidate.created_at <= threshold:
                self._candidates.pop(terminal_id, None)
