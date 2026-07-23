from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

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

    async def evaluate(_event: HookEvent) -> HookResponse:
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


def test_runtime_propagates_exceptions_and_rejects_work_after_shutdown() -> None:
    runtime = WorkflowEvaluationRuntime(max_workers=1)

    async def fail() -> None:
        raise LookupError("evaluation failed")

    with pytest.raises(LookupError, match="evaluation failed"):
        runtime.run(fail())

    runtime.shutdown()
    runtime.shutdown()

    async def succeed() -> str:
        return "ok"

    with pytest.raises(RuntimeError, match="not running"):
        runtime.run(succeed())
