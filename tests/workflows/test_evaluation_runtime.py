from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.workflows.evaluation_runtime import (
    ChildEvaluationCancelled,
    WorkflowEvaluationRuntime,
    WorkflowEvaluationTimeout,
)
from gobby.workflows.hooks import WorkflowHookHandler


def _event(tmp_path: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="runtime-test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": "runtime-test-session"},
        cwd=str(tmp_path),
    )


def test_stalled_to_thread_dependency_respects_evaluation_timeout(tmp_path: Path) -> None:
    runtime = WorkflowEvaluationRuntime(max_workers=1)
    handler = WorkflowHookHandler(timeout=0.05, evaluation_runtime=runtime)
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(timeout=1)

    async def evaluate(
        _event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        del blocking_deadline
        await asyncio.to_thread(block)
        return HookResponse(decision="allow")

    handler._evaluate_rules = evaluate  # type: ignore[method-assign,assignment]
    started_at = time.perf_counter()
    try:
        with pytest.raises(WorkflowEvaluationTimeout):
            handler.evaluate(_event(tmp_path))
        elapsed = time.perf_counter() - started_at

        assert started.wait(timeout=0.2)
        assert elapsed < 0.2
    finally:
        release.set()
        handler.shutdown()


def test_init_waits_for_loop_start_so_immediate_run_succeeds() -> None:
    gate = threading.Event()
    real_new_event_loop = asyncio.new_event_loop

    def gated_loop() -> asyncio.AbstractEventLoop:
        loop = real_new_event_loop()
        original_run_forever = loop.run_forever

        def gated_run_forever() -> None:
            gate.wait(timeout=1)
            original_run_forever()

        loop.run_forever = gated_run_forever  # type: ignore[method-assign]
        return loop

    release_timer = threading.Timer(0.05, gate.set)
    release_timer.start()
    try:
        with patch(
            "gobby.workflows.evaluation_runtime.asyncio.new_event_loop",
            side_effect=gated_loop,
        ):
            runtime = WorkflowEvaluationRuntime(max_workers=1)

        async def probe() -> str:
            return "ok"

        try:
            assert runtime.run(probe()) == "ok"
        finally:
            runtime.shutdown()
    finally:
        gate.set()
        release_timer.cancel()


def test_run_timeout_releases_the_caller_and_cancels_the_coroutine() -> None:
    """Callers are adapter threads from a small pool; a wedge must not pin one."""
    runtime = WorkflowEvaluationRuntime(max_workers=1)
    cancelled = threading.Event()

    async def never_completes() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    started_at = time.perf_counter()
    try:
        with pytest.raises(TimeoutError):
            runtime.run(never_completes(), timeout=0.05)

        assert time.perf_counter() - started_at < 1.0
        assert cancelled.wait(timeout=1.0)
    finally:
        runtime.shutdown()


def test_run_reraises_a_coroutine_timeout_rather_than_treating_it_as_a_wedge() -> None:
    runtime = WorkflowEvaluationRuntime(max_workers=1)

    async def times_out() -> None:
        raise TimeoutError("evaluation exceeded its own deadline")

    try:
        with pytest.raises(TimeoutError, match="its own deadline"):
            runtime.run(times_out(), timeout=5.0)
    finally:
        runtime.shutdown()


def test_evaluate_reports_a_wedged_runtime_as_an_evaluation_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged loop raises nothing of its own, so the hook boundary needs a translation."""
    runtime = WorkflowEvaluationRuntime(max_workers=1)
    handler = WorkflowHookHandler(timeout=0.05, evaluation_runtime=runtime)

    def wedged(
        coroutine: Coroutine[Any, Any, HookResponse],
        *,
        timeout: float | None = None,
    ) -> HookResponse:
        assert timeout == pytest.approx(1.05)
        coroutine.close()
        raise TimeoutError("runtime never scheduled the coroutine")

    monkeypatch.setattr(runtime, "run", wedged)
    try:
        with pytest.raises(WorkflowEvaluationTimeout) as excinfo:
            handler.evaluate(_event(tmp_path))
    finally:
        handler.shutdown()

    assert excinfo.value.timeout_seconds == pytest.approx(1.05)
    assert excinfo.value.session_id == "runtime-test-session"
    assert isinstance(excinfo.value.__cause__, TimeoutError)


def test_runtime_propagates_exceptions_and_rejects_work_after_shutdown() -> None:
    runtime = WorkflowEvaluationRuntime(max_workers=1)
    assert runtime.is_closing is False

    async def fail() -> None:
        raise LookupError("evaluation failed")

    with pytest.raises(LookupError, match="evaluation failed"):
        runtime.run(fail())

    runtime.shutdown()
    runtime.shutdown()
    assert runtime.is_closing is True

    async def succeed() -> str:
        return "ok"

    with pytest.raises(RuntimeError, match="not running"):
        runtime.run(succeed())


def test_run_reports_an_in_band_child_cancel_as_child_evaluation_cancelled() -> None:
    """An in-band child cancel is not a cancel of the evaluation task (#22706).

    A child awaited inside the evaluation can surface its own
    ``asyncio.CancelledError``. Propagation through ``wait_for`` and
    ``wrap_future`` does not identify who initiated the cancellation.
    ``wait_for`` and ``run_coroutine_threadsafe`` propagate the child cancel
    unchanged, so the adapter thread
    would otherwise see an ordinary ``concurrent.futures.CancelledError`` and
    misattribute it to a canceller of the evaluation. ``run()`` must report it
    as ``ChildEvaluationCancelled`` because the evaluation task was never
    cancelled.
    """
    runtime = WorkflowEvaluationRuntime(max_workers=1)

    async def child_never_completes() -> None:
        await asyncio.Event().wait()

    async def cancelled_by_child() -> None:
        child = asyncio.create_task(child_never_completes())
        child.cancel()
        await child
        raise AssertionError("the child cancellation must propagate")

    try:
        with pytest.raises(ChildEvaluationCancelled):
            runtime.run(cancelled_by_child(), timeout=5.0)
    finally:
        runtime.shutdown()


def test_run_reraises_a_genuine_task_cancel_unchanged() -> None:
    """A real cancellation of the evaluation task still reports CancelledError (#22706).

    ``shutdown()`` cancels the pending evaluation task directly, which must not
    be softened into ``ChildEvaluationCancelled``; that would hide a genuine
    cancel and let ``evaluate`` retry an event that was actually cancelled.
    """
    runtime = WorkflowEvaluationRuntime(max_workers=1)
    entered = threading.Event()

    async def hold() -> None:
        entered.set()
        await asyncio.Event().wait()

    def cancel_via_shutdown() -> None:
        entered.wait(timeout=5.0)
        runtime.shutdown()

    canceller = threading.Thread(target=cancel_via_shutdown)
    canceller.start()
    try:
        with pytest.raises(concurrent.futures.CancelledError):
            runtime.run(hold(), timeout=30.0)
    finally:
        canceller.join(timeout=5.0)
        runtime.shutdown()
