from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.rule_evaluator import WorkflowRuleEvaluator
from gobby.servers.routes.mcp.hooks import _run_adapter_hook
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowEvaluationTimeout, WorkflowHookHandler

pytestmark = pytest.mark.unit


def _event(tmp_path: Path, *, session_id: str = "platform-session") -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="external-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": session_id},
        cwd=str(tmp_path),
    )


@pytest.mark.asyncio
def _handler(
    evaluate: Any,
    *,
    timeout: float,
    runtime: WorkflowEvaluationRuntime,
) -> WorkflowHookHandler:
    rule_engine = MagicMock()
    rule_engine.db = MagicMock()
    rule_engine.evaluate = evaluate

    session_vars = MagicMock()
    session_vars.get_variables.return_value = {
        "baseline_dirty_files": [],
        "session_edited_files": [],
    }

    handler = WorkflowHookHandler(timeout=timeout, evaluation_runtime=runtime)
    handler.rule_engine = rule_engine
    handler._session_var_manager = session_vars
    return handler


@pytest.mark.asyncio
async def test_concurrent_sync_evaluations_keep_daemon_loop_responsive(tmp_path: Path) -> None:
    worker_count = 3
    started = 0
    started_lock = threading.Lock()
    all_started = threading.Event()
    release = threading.Event()
    evaluation_threads: list[int] = []
    daemon_thread = threading.get_ident()

    async def blocking_evaluation(
        _event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        del blocking_deadline
        nonlocal started
        evaluation_threads.append(threading.get_ident())
        with started_lock:
            started += 1
            if started == worker_count:
                all_started.set()
        await asyncio.to_thread(release.wait, 1)
        return HookResponse(decision="allow")

    runtime = WorkflowEvaluationRuntime(max_workers=worker_count)
    handler = WorkflowHookHandler(timeout=0.8, evaluation_runtime=runtime)
    handler._evaluate_rules = blocking_evaluation
    event = _event(tmp_path)
    adapter = MagicMock()
    adapter.handle_native.side_effect = lambda *_args: handler.evaluate(event)

    evaluations = [
        asyncio.create_task(_run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=0.9))
        for _ in range(worker_count)
    ]

    try:
        assert await asyncio.to_thread(all_started.wait, 0.4)
    finally:
        release.set()

    try:
        results = await asyncio.gather(*evaluations)
        assert [result.decision for result in results] == ["allow"] * worker_count
        assert evaluation_threads
        assert all(thread_id != daemon_thread for thread_id in evaluation_threads)
    finally:
        handler.shutdown()


@pytest.mark.asyncio
async def test_internal_timeout_cancels_evaluation_and_releases_session_lock(
    tmp_path: Path,
) -> None:
    cancelled = threading.Event()
    evaluation_timeout = 0.2

    async def slow_evaluate(
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        del event, session_id, variables, eval_context, blocking_deadline
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime = WorkflowEvaluationRuntime()
    handler = _handler(slow_evaluate, timeout=evaluation_timeout, runtime=runtime)
    event = _event(tmp_path)
    adapter = MagicMock()
    adapter.handle_native.side_effect = lambda *_args: handler.evaluate(event)

    try:
        with pytest.raises(WorkflowEvaluationTimeout) as raised:
            await _run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=1.0)

        error = raised.value
        assert cancelled.wait(timeout=0.5)
        assert error.event_type == HookEventType.BEFORE_TOOL.value
        assert error.session_id == "platform-session"
        assert error.timeout_seconds == evaluation_timeout
        assert error.queue_duration_seconds is not None
        assert error.queue_duration_seconds >= 0
        assert error.execution_duration_seconds is not None
        assert error.execution_duration_seconds >= evaluation_timeout
        with handler._eval_locks_lock:
            lock_state = handler._eval_locks["platform-session"]
            assert lock_state.references == 0
            assert lock_state.lock.acquire(blocking=False)
            lock_state.lock.release()

        async def fast_evaluate(
            *,
            event: HookEvent,
            session_id: str,
            variables: dict[str, Any],
            eval_context: dict[str, Any] | None = None,
            blocking_deadline: BlockingEffectDeadline | None = None,
        ) -> HookResponse:
            del event, session_id, variables, eval_context, blocking_deadline
            return HookResponse(decision="allow")

        handler.rule_engine.evaluate = fast_evaluate
        response = await _run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=0.5)
        assert response.decision == "allow"
    finally:
        handler.shutdown()


@pytest.mark.asyncio
async def test_timeout_while_waiting_for_session_lock_never_executes_queued_event(
    tmp_path: Path,
) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    entered: list[str] = []

    async def evaluate(
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        del session_id, variables, eval_context, blocking_deadline
        name = str(event.data["name"])
        entered.append(name)
        if name == "first":
            first_started.set()
            await asyncio.to_thread(release_first.wait, 1)
        return HookResponse(decision="allow")

    runtime = WorkflowEvaluationRuntime()
    handler = _handler(evaluate, timeout=0.5, runtime=runtime)
    first_event = _event(tmp_path)
    first_event.data["name"] = "first"
    second_event = _event(tmp_path)
    second_event.data["name"] = "second"
    adapter = MagicMock()
    adapter.handle_native.side_effect = lambda payload, _manager: {
        "decision": handler.evaluate(payload["event"]).decision
    }

    try:
        first = asyncio.create_task(
            _run_adapter_hook(
                adapter,
                {"event": first_event},
                MagicMock(),
                timeout_seconds=0.6,
            )
        )
        assert await asyncio.to_thread(first_started.wait, 0.2)

        handler.timeout = 0.02
        with pytest.raises(WorkflowEvaluationTimeout):
            await _run_adapter_hook(
                adapter,
                {"event": second_event},
                MagicMock(),
                timeout_seconds=0.5,
            )

        assert entered == ["first"]
        release_first.set()
        assert (await first)["decision"] == "allow"
    finally:
        release_first.set()
        handler.shutdown()


def test_rule_evaluator_propagates_workflow_timeout_without_logging() -> None:
    timeout = WorkflowEvaluationTimeout(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="platform-session",
        timeout_seconds=15,
    )

    class TimeoutHandler:
        calls = 0

        def handle(
            self,
            _event: HookEvent,
            *,
            blocking_deadline: BlockingEffectDeadline | None = None,
        ) -> HookResponse:
            del blocking_deadline
            self.calls += 1
            raise timeout

    class RecordingLogger:
        def __init__(self) -> None:
            self.errors: list[tuple[object, ...]] = []

        def error(self, *args: object, **_kwargs: object) -> None:
            self.errors.append(args)

    def unexpected_dispatch(
        _calls: list[dict[str, Any]], _event: HookEvent
    ) -> list[dict[str, Any]]:
        raise AssertionError("timeout must propagate before MCP dispatch")

    def unexpected_format(_result: dict[str, Any]) -> str:
        raise AssertionError("timeout must propagate before discovery formatting")

    workflow_handler = TimeoutHandler()
    logger = RecordingLogger()
    evaluator = WorkflowRuleEvaluator(
        workflow_handler=workflow_handler,
        dispatch_mcp_calls=unexpected_dispatch,
        format_discovery_result=unexpected_format,
        database=MagicMock(),
        logger=logger,
    )

    with pytest.raises(WorkflowEvaluationTimeout, match="event="):
        evaluator.evaluate(_event(Path("/tmp")))

    assert workflow_handler.calls == 1
    assert logger.errors == []
