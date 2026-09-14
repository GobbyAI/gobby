from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool, PoolTimeout

from gobby.hooks.adapter_execution import run_adapter_hook as _run_adapter_hook
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.rule_evaluator import WorkflowRuleEvaluator
from gobby.storage.hub import postgres_pool
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime, WorkflowEvaluationTimeout
from gobby.workflows.hooks import WorkflowHookHandler

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
async def test_concurrent_sync_evaluations_keep_daemon_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(handler, "_evaluate_rules", blocking_evaluation)
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
        assert [cast(HookResponse, result).decision for result in results] == [
            "allow"
        ] * worker_count
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
            raise AssertionError("The cancellation sentinel must never be released")
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
            assert not lock_state.lock.locked()

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
        response = cast(
            HookResponse,
            await _run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=0.5),
        )
        assert response.decision == "allow"
    finally:
        handler.shutdown()


@pytest.mark.asyncio
async def test_database_acquisition_timeout_does_not_retry_inside_workflow(
    tmp_path: Path,
) -> None:
    class TimeoutOncePool:
        def __init__(self) -> None:
            self.connection_calls = 0
            self.check_calls = 0
            self.timeouts: list[float | None] = []

        @contextmanager
        def connection(self, timeout: float | None = None) -> Any:
            self.connection_calls += 1
            self.timeouts.append(timeout)
            if self.connection_calls == 1:
                assert timeout is not None
                entered_pool.set()
                release_pool.wait(timeout=1.0)
                raise PoolTimeout("pool busy")
            yield object()

        def check(self) -> None:
            self.check_calls += 1
            release_check.wait(timeout=0.3)

    pool = TimeoutOncePool()
    entered_pool = threading.Event()
    release_pool = threading.Event()
    release_check = threading.Event()

    def acquire_connection() -> None:
        with postgres_pool.pool_connection(
            cast(ConnectionPool[Any], pool),
            lambda: {},
        ):
            pass

    async def evaluate(
        *,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        eval_context: dict[str, Any] | None = None,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        del event, session_id, variables, eval_context, blocking_deadline
        await asyncio.to_thread(acquire_connection)
        return HookResponse(decision="allow")

    runtime = WorkflowEvaluationRuntime(max_workers=1)
    evaluation_timeout = 0.08
    handler = _handler(evaluate, timeout=evaluation_timeout, runtime=runtime)
    event = _event(tmp_path)
    adapter = MagicMock()
    adapter.handle_native.side_effect = lambda *_args: handler.evaluate(event)

    try:
        started = time.monotonic()
        with pytest.raises(WorkflowEvaluationTimeout):
            await _run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=1.0)
        first_elapsed = time.monotonic() - started

        assert pool.connection_calls == 1
        assert len(pool.timeouts) == 1
        assert pool.timeouts[0] is not None
        assert 0 < pool.timeouts[0] <= evaluation_timeout
        assert evaluation_timeout <= first_elapsed < evaluation_timeout + 0.08
        assert entered_pool.is_set()
        release_pool.set()

        next_started = time.monotonic()
        response = cast(
            HookResponse,
            await _run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=0.5),
        )
        assert response.decision == "allow"
        assert pool.connection_calls == 2
        assert pool.check_calls == 0
        assert time.monotonic() - next_started < evaluation_timeout
    finally:
        release_pool.set()
        release_check.set()
        handler.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_exhausted_pool_releases_workflow_worker(tmp_path: Path) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for the PostgreSQL pool test")
    finished = threading.Event()
    with ConnectionPool[Any](dsn, min_size=1, max_size=1, timeout=0.05) as pool:
        pool.wait(timeout=3.0)

        def acquire() -> None:
            try:
                with postgres_pool.pool_connection(
                    pool, pool.get_stats, acquire_timeout_seconds=0.05
                ):
                    pass
            finally:
                finished.set()

        async def evaluate(**_kwargs: Any) -> HookResponse:
            await asyncio.to_thread(acquire)
            return HookResponse(decision="allow")

        runtime = WorkflowEvaluationRuntime(max_workers=1)
        handler = _handler(evaluate, timeout=0.4, runtime=runtime)
        event = _event(tmp_path)
        adapter = MagicMock()
        adapter.handle_native.side_effect = lambda *_args: handler.evaluate(event)
        try:
            with pool.connection():
                started = time.monotonic()
                with pytest.raises(PoolTimeout):
                    await _run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=1.0)
                assert time.monotonic() - started < 0.3
                assert finished.is_set()

            # psycopg_pool discards expired queue entries when a connection returns.
            assert pool.get_stats()["requests_waiting"] == 0
            response = cast(
                HookResponse,
                await _run_adapter_hook(adapter, {}, MagicMock(), timeout_seconds=1.0),
            )
            assert response.decision == "allow"
            assert pool.get_stats()["pool_available"] == 1
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

    def unexpected_dispatch(
        _calls: list[dict[str, Any]], _event: HookEvent
    ) -> list[dict[str, Any]]:
        raise AssertionError("timeout must propagate before MCP dispatch")

    def unexpected_format(_result: dict[str, Any]) -> str:
        raise AssertionError("timeout must propagate before discovery formatting")

    workflow_handler = TimeoutHandler()
    logger = MagicMock(spec=logging.Logger)
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
    logger.error.assert_not_called()
