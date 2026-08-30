from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime
from typing import NoReturn

import pytest

from gobby.agents import terminal_delivery
from gobby.agents.capture import (
    CaptureTerminationResult,
    KillOutcome,
    TerminationErrorCode,
    _async_storage_call,
    capture_then_kill_async,
    capture_then_kill_sync,
)
from gobby.storage.agents import AgentRun, AgentRunStatus


def _run(run_id: str, *, result: str | None = None) -> AgentRun:
    now = datetime.now(UTC)
    return AgentRun(
        id=run_id,
        parent_session_id="parent",
        provider="codex",
        prompt="test",
        status="running",
        created_at=now,
        updated_at=now,
        result=result,
        terminal_id="shared-session",
    )


@pytest.mark.asyncio
async def test_async_storage_call_rejects_closed_admission_with_explicit_run_id() -> None:
    invoked = False

    def storage_callback() -> str:
        nonlocal invoked
        invoked = True
        return "unexpected"

    terminal_delivery.close_terminal_delivery_admission()
    try:
        with pytest.raises(
            terminal_delivery.TerminalDeliveryAdmissionClosedError,
            match="explicit-run-id",
        ):
            await _async_storage_call("explicit-run-id", storage_callback)
    finally:
        terminal_delivery.reopen_terminal_delivery_admission()

    assert invoked is False


class FakeCaptureStorage:
    def __init__(self, *runs: AgentRun) -> None:
        self.runs = {run.id: run for run in runs}
        self.events: list[str] = []
        self.fail_intent = False

    def get(self, run_id: str) -> AgentRun | None:
        return self.runs.get(run_id)

    def record_termination_intent(
        self,
        run_id: str,
        *,
        action: str,
        reason: str | None = None,
        result_prefix: str | None = None,
    ) -> AgentRun | None:
        self.events.append(f"intent:{run_id}:{action}")
        if self.fail_intent:
            raise RuntimeError("database unavailable")
        run = self.runs.get(run_id)
        if run is None or run.status not in ("pending", "running"):
            return None
        result = run.result
        if not result and result_prefix:
            result = result_prefix
        updated = replace(
            run,
            result=result,
            pending_terminal_action=action,
            pending_terminal_reason=reason,
        )
        self.runs[run_id] = updated
        return updated

    def replace_capture_slot(
        self,
        run_id: str,
        *,
        capture_id: str,
        expected_revision: int,
        marker: str,
        slot_content: str,
    ) -> AgentRun | None:
        self.events.append(f"persist:{run_id}")
        run = self.runs[run_id]
        if run.status not in ("pending", "running"):
            return None
        if run.capture_id is None:
            if run.capture_revision != 0:
                return None
            prefix = f"{run.result}\n\n" if run.result else ""
        else:
            if run.capture_id != capture_id or run.capture_revision != expected_revision:
                return None
            marker_at = (run.result or "").find(marker)
            if marker_at < 0:
                return None
            prefix = (run.result or "")[:marker_at]
        updated = replace(
            run,
            result=f"{prefix}{slot_content}",
            capture_id=capture_id,
            capture_revision=run.capture_revision + 1,
        )
        self.runs[run_id] = updated
        return updated

    def _terminal(self, run_id: str, status: AgentRunStatus) -> AgentRun | None:
        run = self.runs[run_id]
        if run.status not in ("pending", "running"):
            return None
        self.events.append(f"terminal:{run_id}:{status}")
        updated = replace(
            run,
            status=status,
            pending_terminal_action=None,
            pending_terminal_reason=None,
            terminal_id=None,
        )
        self.runs[run_id] = updated
        return updated

    def complete(self, run_id: str, result: str | None = None) -> AgentRun | None:
        return self._terminal(run_id, "success")

    def fail(
        self,
        run_id: str,
        error: str,
        tool_calls_count: int = 0,
        turns_used: int = 0,
        result: str | None = None,
    ) -> AgentRun | None:
        return self._terminal(run_id, "error")

    def timeout(
        self,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
        result: str | None = None,
    ) -> AgentRun | None:
        return self._terminal(run_id, "timeout")

    def cancel(
        self,
        run_id: str,
        *,
        terminal_reason: str | None = None,
        result: str | None = None,
    ) -> AgentRun | None:
        return self._terminal(run_id, "cancelled")


def test_kill_failure_retries_with_capture_slot_replacement() -> None:
    storage = FakeCaptureStorage(_run("retry"))
    alive = True
    pane = "first output"

    def capture() -> str:
        storage.events.append("capture")
        return pane

    def failed_kill() -> bool:
        storage.events.append("kill:false")
        return False

    first = capture_then_kill_sync(
        storage=storage,
        run_id="retry",
        session_name="retry-session",
        action="fail",
        reason="watchdog",
        session_alive=lambda: alive,
        capture=capture,
        kill=failed_kill,
    )
    assert first.error_code == TerminationErrorCode.KILL_FAILED
    assert storage.runs["retry"].status == "running"
    assert storage.runs["retry"].capture_revision == 1

    pane = "first output\nprinted after failed kill"

    def successful_kill() -> bool:
        nonlocal alive
        storage.events.append("kill:true")
        alive = False
        return True

    second = capture_then_kill_sync(
        storage=storage,
        run_id="retry",
        session_name="retry-session",
        action="fail",
        reason="watchdog",
        session_alive=lambda: alive,
        capture=capture,
        kill=successful_kill,
    )
    assert second.success
    assert second.kill_outcome == KillOutcome.KILLED
    final = storage.runs["retry"]
    assert final.status == "error"
    assert final.capture_revision == 2
    assert final.result is not None
    assert "printed after failed kill" in final.result
    assert final.result.count("--- GOBBY TMUX CAPTURE ") == 1
    assert storage.events.index("persist:retry") < storage.events.index("kill:false")
    assert storage.events[-1] == "terminal:retry:error"


def test_retry_after_kill_preserves_original_capture() -> None:
    storage = FakeCaptureStorage(_run("crash"))
    alive = True
    capture_calls = 0

    def capture() -> str:
        nonlocal capture_calls
        capture_calls += 1
        return "complete capture"

    def kill() -> bool:
        nonlocal alive
        alive = False
        return True

    def crash_terminal(_action: str, _reason: str | None) -> AgentRun | None:
        raise RuntimeError("crash after kill")

    first = capture_then_kill_sync(
        storage=storage,
        run_id="crash",
        session_name="crash-session",
        action="complete",
        session_alive=lambda: alive,
        capture=capture,
        kill=kill,
        terminalize=crash_terminal,
    )
    assert first.error_code == TerminationErrorCode.TERMINAL_TRANSITION_FAILED
    original_result = storage.runs["crash"].result

    second = capture_then_kill_sync(
        storage=storage,
        run_id="crash",
        session_name="crash-session",
        action="complete",
        session_alive=lambda: alive,
        capture=capture,
        kill=kill,
    )
    assert second.success
    assert second.kill_outcome == KillOutcome.ALREADY_ABSENT
    assert capture_calls == 1
    assert storage.runs["crash"].result == original_result


def test_persistence_failure_never_kills() -> None:
    storage = FakeCaptureStorage(_run("db-failure"))
    storage.fail_intent = True
    killed = False

    def kill() -> bool:
        nonlocal killed
        killed = True
        return True

    result = capture_then_kill_sync(
        storage=storage,
        run_id="db-failure",
        session_name="db-failure-session",
        action="fail",
        session_alive=lambda: True,
        capture=lambda: "only copy",
        kill=kill,
    )
    assert result.error_code == TerminationErrorCode.CAPTURE_PERSIST_FAILED
    assert not killed


def test_success_with_summary_skips_capture() -> None:
    storage = FakeCaptureStorage(_run("summary", result="durable summary"))
    alive = True
    capture_calls = 0

    def capture() -> str:
        nonlocal capture_calls
        capture_calls += 1
        return "pane"

    def kill() -> bool:
        nonlocal alive
        alive = False
        return True

    result = capture_then_kill_sync(
        storage=storage,
        run_id="summary",
        session_name="summary-session",
        action="complete",
        session_alive=lambda: alive,
        capture=capture,
        kill=kill,
    )
    assert result.success
    assert capture_calls == 0
    assert storage.runs["summary"].result == "durable summary"


@pytest.mark.asyncio
async def test_async_lock_cancellation_deadline_and_next_acquisition() -> None:
    storage = FakeCaptureStorage(
        _run("sync-holder"),
        _run("cancelled", result="summary"),
        _run("deadline", result="summary"),
        _run("next", result="summary"),
    )
    holder_entered = threading.Event()
    release_holder = threading.Event()
    holder_alive = True

    async def not_alive() -> bool:
        return False

    async def unused_capture() -> str:
        return "unused"

    async def successful_kill() -> bool:
        return True

    async def next_loop_turn() -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        loop.call_soon(ready.set_result, None)
        await ready

    def blocking_kill() -> bool:
        nonlocal holder_alive
        holder_entered.set()
        assert release_holder.wait(timeout=2)
        holder_alive = False
        return True

    holder_result: list[CaptureTerminationResult] = []

    def hold_sync_lock() -> None:
        holder_result.append(
            capture_then_kill_sync(
                storage=storage,
                run_id="sync-holder",
                session_name="shared-session",
                action="complete",
                session_alive=lambda: holder_alive,
                capture=lambda: "holder output",
                kill=blocking_kill,
            )
        )

    thread = threading.Thread(target=hold_sync_lock)
    thread.start()
    assert await asyncio.to_thread(holder_entered.wait, 1)

    cancelled = asyncio.create_task(
        capture_then_kill_async(
            storage=storage,
            run_id="cancelled",
            session_name="shared-session",
            action="complete",
            session_alive=not_alive,
            capture=unused_capture,
            kill=successful_kill,
            lock_timeout=1,
        )
    )
    heartbeats = 0
    for _ in range(5):
        await next_loop_turn()
        heartbeats += 1
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled, timeout=0.5)
    assert heartbeats == 5

    deadline = await capture_then_kill_async(
        storage=storage,
        run_id="deadline",
        session_name="shared-session",
        action="complete",
        session_alive=not_alive,
        capture=unused_capture,
        kill=successful_kill,
        lock_timeout=0.05,
    )
    assert deadline.error_code == TerminationErrorCode.CAPTURE_LOCK_TIMEOUT

    release_holder.set()
    await asyncio.to_thread(thread.join, 1)
    assert not thread.is_alive()
    assert holder_result and holder_result[0].success

    next_result = await capture_then_kill_async(
        storage=storage,
        run_id="next",
        session_name="shared-session",
        action="complete",
        session_alive=not_alive,
        capture=unused_capture,
        kill=successful_kill,
    )
    assert next_result.success


def _must_not_run() -> NoReturn:
    raise AssertionError("termination side effects must not run on intent rejection")


async def _async_must_not_run() -> NoReturn:
    raise AssertionError("termination side effects must not run on intent rejection")


def test_sync_intent_rejection_distinguishes_already_terminal() -> None:
    run = replace(_run("run-terminal-sync"), status="success")
    storage = FakeCaptureStorage(run)

    result = capture_then_kill_sync(
        storage=storage,
        run_id="run-terminal-sync",
        session_name="already-terminal-sync",
        action="fail",
        reason="watchdog",
        session_alive=_must_not_run,
        capture=_must_not_run,
        kill=_must_not_run,
    )

    assert result.success is False
    assert result.error_code == TerminationErrorCode.ALREADY_TERMINAL
    assert result.error == "agent run already terminal (status=success)"
    assert result.run is run


def test_sync_intent_rejection_reports_missing_run() -> None:
    storage = FakeCaptureStorage()

    result = capture_then_kill_sync(
        storage=storage,
        run_id="run-absent-sync",
        session_name="missing-run-sync",
        action="complete",
        session_alive=_must_not_run,
        capture=_must_not_run,
        kill=_must_not_run,
    )

    assert result.success is False
    assert result.error_code == TerminationErrorCode.CAPTURE_PERSIST_FAILED
    assert result.error == "agent run not found"
    assert result.run is None


@pytest.mark.asyncio
async def test_async_intent_rejection_distinguishes_already_terminal() -> None:
    run = replace(_run("run-terminal-async"), status="cancelled")
    storage = FakeCaptureStorage(run)

    result = await capture_then_kill_async(
        storage=storage,
        run_id="run-terminal-async",
        session_name="already-terminal-async",
        action="fail",
        reason="watchdog",
        session_alive=_async_must_not_run,
        capture=_async_must_not_run,
        kill=_async_must_not_run,
    )

    assert result.success is False
    assert result.error_code == TerminationErrorCode.ALREADY_TERMINAL
    assert result.error == "agent run already terminal (status=cancelled)"
    assert result.run is run


@pytest.mark.asyncio
async def test_async_intent_rejection_reports_missing_run() -> None:
    storage = FakeCaptureStorage()

    result = await capture_then_kill_async(
        storage=storage,
        run_id="run-absent-async",
        session_name="missing-run-async",
        action="complete",
        session_alive=_async_must_not_run,
        capture=_async_must_not_run,
        kill=_async_must_not_run,
    )

    assert result.success is False
    assert result.error_code == TerminationErrorCode.CAPTURE_PERSIST_FAILED
    assert result.error == "agent run not found"
    assert result.run is None


@pytest.mark.asyncio
async def test_truncation_metadata_is_persisted() -> None:
    from gobby.agents.capture import parse_capture_slot, terminate_managed_runtime_async
    from gobby.terminals.runtime import SnapshotResult
    from tests.terminals.fakes import FakeRuntime, make_memory_terminal

    storage = FakeCaptureStorage(_run("trunc"))
    terminal = make_memory_terminal()
    runtime = FakeRuntime()
    runtime.snapshot_full_result = SnapshotResult(
        text="tail",
        truncated=True,
        dropped_bytes=None,
        total_bytes=None,
    )

    result = await terminate_managed_runtime_async(
        storage=storage,
        run=storage.runs["trunc"],
        terminal=terminal,
        runtime=runtime,
        action="fail",
        reason="killed",
    )
    assert result.success
    parsed = parse_capture_slot(storage.runs["trunc"].result or "")
    assert parsed.truncated is True
    assert parsed.dropped_bytes is None
    assert parsed.total_bytes is None
    assert parsed.text == "tail"

    storage = FakeCaptureStorage(_run("full"))
    runtime.snapshot_full_result = SnapshotResult(
        text="whole",
        truncated=False,
        dropped_bytes=0,
        total_bytes=5,
    )
    result = await terminate_managed_runtime_async(
        storage=storage,
        run=storage.runs["full"],
        terminal=terminal,
        runtime=runtime,
        action="complete",
    )
    assert result.success
    parsed = parse_capture_slot(storage.runs["full"].result or "")
    assert parsed.truncated is False
    assert parsed.dropped_bytes == 0
    assert parsed.total_bytes == 5
    assert parsed.text == "whole"


@pytest.mark.asyncio
async def test_terminate_managed_runtime_kills_remain_on_exit_session() -> None:
    from gobby.agents.capture import KillOutcome, terminate_managed_runtime_async
    from gobby.terminals.runtime import SnapshotResult
    from tests.terminals.fakes import FakeRuntime, make_memory_terminal

    class RemainOnExitRuntime(FakeRuntime):
        async def is_live(self, terminal: object) -> bool:
            del terminal
            return False

        async def session_present(self, terminal: object) -> bool:
            return getattr(terminal, "id", None) not in self.killed_ids

    storage = FakeCaptureStorage(_run("remain"))
    terminal = make_memory_terminal(session_name="gobby-remain")
    runtime = RemainOnExitRuntime()
    runtime.snapshot_full_result = SnapshotResult(
        text="orphan scrollback",
        truncated=False,
        dropped_bytes=0,
        total_bytes=18,
    )

    result = await terminate_managed_runtime_async(
        storage=storage,
        run=storage.runs["remain"],
        terminal=terminal,
        runtime=runtime,
        action="complete",
    )
    assert result.success
    assert result.kill_outcome == KillOutcome.KILLED
    assert runtime.killed == ["gobby-remain"]
    assert await runtime.session_present(terminal) is False
