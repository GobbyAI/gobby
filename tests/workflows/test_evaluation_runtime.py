from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowEvaluationTimeout, WorkflowHookHandler


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
