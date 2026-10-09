"""Provider-specific mediated terminal interruption correlation."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event, get_ident
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from gobby.sessions.terminal_turn_observer import TerminalTurnObserver, WriteOutcome
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

_AGY_INTERRUPTED = "⎿  Interrupted · What should Antigravity CLI do instead?"


class _Terminals:
    def __init__(self, terminal_id: str, session_id: str) -> None:
        self._terminal_id = terminal_id
        self._session_id = session_id
        self.db = SimpleNamespace(fetchone=lambda *_args, **_kwargs: None)

    def get(self, terminal_id: str) -> SimpleNamespace | None:
        if terminal_id != self._terminal_id:
            return None
        return SimpleNamespace(session_id=self._session_id)


def test_concurrent_output_waits_for_interrupt_candidate_recording(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    _sessions, session_id, _lifecycle, observer = _observer(
        temp_db, sample_project["id"], provider="agy"
    )
    entered = Event()
    release = Event()

    class BlockingTerminals(_Terminals):
        def get(self, terminal_id: str) -> SimpleNamespace | None:
            entered.set()
            assert release.wait(5)
            return super().get(terminal_id)

    observer.set_terminal_manager(BlockingTerminals("terminal-1", session_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        recorded = pool.submit(observer.record_mediated_input, "terminal-1", "\x03", "delivered")
        assert entered.wait(5)
        observed = pool.submit(observer.observe_output, "terminal-1", _AGY_INTERRUPTED)
        try:
            with pytest.raises(FutureTimeoutError):
                observed.result(timeout=0.1)
        finally:
            release.set()
        assert recorded.result(timeout=5) is True
        assert observed.result(timeout=5) is True


@pytest.mark.asyncio
async def test_async_observer_offloads_lookup_and_skips_idle_output(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    _sessions, session_id, _lifecycle, observer = _observer(
        temp_db, sample_project["id"], provider="agy"
    )
    lookup_threads: list[int] = []

    class RecordingTerminals(_Terminals):
        def get(self, terminal_id: str) -> SimpleNamespace | None:
            lookup_threads.append(get_ident())
            return super().get(terminal_id)

    terminals = RecordingTerminals("terminal-1", session_id)

    def unexpected_query(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("idle output must not query terminals")

    terminals.db = SimpleNamespace(fetchone=unexpected_query)
    observer.set_terminal_manager(terminals)
    assert await observer.observe_output_async("terminal-1", _AGY_INTERRUPTED) is False
    assert await observer.observe_run_output_async("run-1", _AGY_INTERRUPTED) is False
    with patch(
        "gobby.sessions.terminal_turn_observer.asyncio.to_thread",
        side_effect=AssertionError("ordinary input must not use a worker"),
    ):
        assert await observer.record_mediated_input_async("terminal-1", "x", "delivered") is False
    assert lookup_threads == []
    assert await observer.record_mediated_input_async("terminal-1", "\x03", "delivered") is True
    assert lookup_threads and lookup_threads[0] != get_ident()
    assert await observer.observe_output_async("terminal-1", _AGY_INTERRUPTED) is True


async def test_cancelled_input_keeps_worker_order_and_socket_loop_live(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    _sessions, session_id, _lifecycle, observer = _observer(
        temp_db, sample_project["id"], provider="agy"
    )
    entered = Event()
    release = Event()
    heartbeat = Event()

    class BlockingTerminals(_Terminals):
        def get(self, terminal_id: str) -> SimpleNamespace | None:
            entered.set()
            assert release.wait(5)
            return super().get(terminal_id)

    observer.set_terminal_manager(BlockingTerminals("terminal-1", session_id))

    def release_after_heartbeat() -> bool:
        saw_heartbeat = heartbeat.wait(2)
        release.set()
        return saw_heartbeat

    with ThreadPoolExecutor(max_workers=1) as pool:
        input_task = asyncio.create_task(
            observer.record_mediated_input_async("terminal-1", "\x03", "delivered")
        )
        assert await asyncio.to_thread(entered.wait, 5)
        input_task.cancel()
        await asyncio.sleep(0)
        output_task = asyncio.create_task(
            observer.observe_output_async("terminal-1", _AGY_INTERRUPTED)
        )
        asyncio.get_running_loop().call_soon(heartbeat.set)
        release_future = pool.submit(release_after_heartbeat)
        input_task.cancel()  # A second cancellation must not release ordering early.
        try:
            with pytest.raises(asyncio.CancelledError):
                await input_task
            assert await output_task is True
            assert release_future.result(timeout=5) is True
        finally:
            release.set()


def _observer(
    temp_db: HubDatabase,
    project_id: str,
    *,
    provider: str,
    clock: list[float] | None = None,
) -> tuple[SessionManager, str, TurnLifecycleReducer, TerminalTurnObserver]:
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id=f"{provider}-mediated-interrupt",
        machine_id=None,
        source=provider,
        project_id=project_id,
    )
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(
        session.id,
        TurnEvidence(source=provider, provider_turn_key="turn-1"),
    )
    terminals = _Terminals("terminal-1", session.id)
    observer = TerminalTurnObserver(
        sessions,
        lifecycle,
        terminal_manager=terminals,
        timeout_seconds=5,
        clock=(lambda: clock[0]) if clock is not None else None,
    )
    return sessions, session.id, lifecycle, observer


@pytest.mark.parametrize("payload", ["\x03", "\x1b"])
@pytest.mark.parametrize("outcome", ["delivered", "indeterminate"])
def test_mediated_key_and_current_output_interrupt_turn(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    payload: str,
    outcome: WriteOutcome,
) -> None:
    sessions, session_id, _, observer = _observer(temp_db, sample_project["id"], provider="agy")

    assert observer.record_mediated_input("terminal-1", payload, outcome) is True
    assert observer.observe_output("terminal-1", f"\x1b[31m{_AGY_INTERRUPTED}\x1b[0m") is True
    updated = sessions.get(session_id)
    assert updated is not None
    assert updated.status == "interrupted"


def test_rejects_refused_missing_stale_and_intervening_input(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions, session_id, lifecycle, observer = _observer(
        temp_db, sample_project["id"], provider="agy"
    )

    assert observer.record_mediated_input("terminal-1", "\x03", "refused") is False
    assert observer.observe_output("terminal-1", _AGY_INTERRUPTED) is False
    assert observer.record_mediated_input("terminal-1", "\x03", "delivered") is True
    assert observer.record_mediated_input("terminal-1", "x", "delivered") is False
    assert observer.observe_output("terminal-1", _AGY_INTERRUPTED) is False

    assert observer.record_mediated_input("terminal-1", "\x1b", "delivered") is True
    lifecycle.begin_turn(
        session_id,
        TurnEvidence(source="agy", provider_turn_key="turn-2"),
    )
    assert observer.observe_output("terminal-1", _AGY_INTERRUPTED) is False
    updated = sessions.get(session_id)
    assert updated is not None
    assert updated.status == "active"


def test_agy_requires_correlated_interruption_output_not_artifact_ui(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions, session_id, _, observer = _observer(temp_db, sample_project["id"], provider="agy")

    assert (
        observer.observe_output(
            "terminal-1", "⎿  Interrupted · What should Antigravity CLI do instead?"
        )
        is False
    )
    assert observer.record_mediated_input("terminal-1", "\x1b", "delivered") is True
    assert observer.observe_output("terminal-1", "Artifact review · Esc to close") is False
    active = sessions.get(session_id)
    assert active is not None
    assert active.status == "active"
    assert (
        observer.observe_output(
            "terminal-1", "⎿  Interrupted · What should Antigravity CLI do instead?"
        )
        is True
    )
    interrupted = sessions.get(session_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"


def test_candidate_expires_without_terminal_effect(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    clock = [10.0]
    sessions, session_id, _, observer = _observer(
        temp_db, sample_project["id"], provider="agy", clock=clock
    )
    assert observer.record_mediated_input("terminal-1", "\x03", "delivered") is True
    clock[0] = 16.0

    assert observer.observe_output("terminal-1", _AGY_INTERRUPTED) is False
    updated = sessions.get(session_id)
    assert updated is not None
    assert updated.status == "active"
