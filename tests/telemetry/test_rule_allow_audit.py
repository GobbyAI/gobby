"""Focused tests for non-blocking rule allow telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from unittest.mock import AsyncMock, patch

import pytest

import gobby.telemetry.rule_allow_audit as rule_allow_audit_module
from gobby.telemetry.rule_allow_audit import (
    RuleAllowAudit,
    record_rule_evaluation,
    shutdown_rule_allow_audit,
)
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime


def _record(audit: RuleAllowAudit) -> bool:
    return audit.record(
        rule_name="allow-rule",
        event="before_tool",
        session_id="session-1",
        latency_ms=12.5,
    )


@pytest.mark.asyncio
async def test_allow_audit_queue_drops_newest_when_full() -> None:
    written: list[dict[str, object]] = []

    async def writer(line: str) -> None:
        written.append(json.loads(line))

    audit = RuleAllowAudit(capacity=2, shutdown_timeout_seconds=1.0, writer=writer)
    with patch("gobby.telemetry.rule_allow_audit.inc_counter") as inc_counter:
        assert [_record(audit), _record(audit), _record(audit)] == [True, True, False]
        await audit.close()

    assert len(written) == 2
    assert written[0] | {"timestamp": "ignored"} == {
        "event": "before_tool",
        "latency_ms": 12.5,
        "result": "allow",
        "rule_name": "allow-rule",
        "session_id": "session-1",
        "timestamp": "ignored",
    }
    inc_counter.assert_called_once_with(
        "rule_allow_audit_dropped_lines_total",
        attributes={"reason": "overflow"},
    )


@pytest.mark.asyncio
async def test_allow_audit_write_errors_are_counted_and_warning_is_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_writer(_line: str) -> None:
        raise OSError("disk full")

    audit = RuleAllowAudit(capacity=2, shutdown_timeout_seconds=1.0, writer=failing_writer)
    caplog.set_level(logging.WARNING, logger="gobby.telemetry.rule_allow_audit")
    with patch("gobby.telemetry.rule_allow_audit.inc_counter") as inc_counter:
        assert _record(audit)
        assert _record(audit)
        await audit.close()

    assert inc_counter.call_count == 2
    assert (
        sum("Failed to write rule allow audit line" in record.message for record in caplog.records)
        == 1
    )


@pytest.mark.asyncio
async def test_allow_audit_shutdown_counts_in_flight_line_after_deadline() -> None:
    started = asyncio.Event()

    async def blocked_writer(_line: str) -> None:
        started.set()
        await asyncio.Event().wait()

    audit = RuleAllowAudit(capacity=1, shutdown_timeout_seconds=0.01, writer=blocked_writer)
    with patch("gobby.telemetry.rule_allow_audit.inc_counter") as inc_counter:
        assert _record(audit)
        await started.wait()
        await audit.close()

    inc_counter.assert_called_once_with(
        "rule_allow_audit_dropped_lines_total",
        amount=1,
        attributes={"reason": "shutdown"},
    )


@pytest.mark.asyncio
async def test_allow_audit_close_drains_writer_on_workflow_runtime_loop() -> None:
    written: list[dict[str, object]] = []

    async def writer(line: str) -> None:
        written.append(json.loads(line))

    audit = RuleAllowAudit(capacity=3, shutdown_timeout_seconds=1.0, writer=writer)
    runtime = WorkflowEvaluationRuntime()

    async def accept_records() -> asyncio.AbstractEventLoop:
        assert [_record(audit), _record(audit), _record(audit)] == [True, True, True]
        return asyncio.get_running_loop()

    try:
        owner_loop = await asyncio.to_thread(runtime.run, accept_records())
        assert owner_loop is not asyncio.get_running_loop()
        await audit.close()
    finally:
        await asyncio.to_thread(runtime.shutdown)

    assert len(written) == 3
    assert not audit.started


@pytest.mark.asyncio
async def test_allow_audit_close_counts_residual_records_after_owner_loop_closes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()

    async def blocked_writer(_line: str) -> None:
        started.set()
        await asyncio.Event().wait()

    audit = RuleAllowAudit(capacity=2, shutdown_timeout_seconds=1.0, writer=blocked_writer)
    runtime = WorkflowEvaluationRuntime()

    async def accept_records() -> None:
        assert _record(audit)
        assert _record(audit)

    caplog.set_level(logging.WARNING)
    with patch("gobby.telemetry.rule_allow_audit.inc_counter") as inc_counter:
        await asyncio.to_thread(runtime.run, accept_records())
        assert await asyncio.to_thread(started.wait, 1.0)
        writer_task = audit._writer_task
        assert writer_task is not None
        await asyncio.to_thread(runtime.shutdown)
        assert writer_task.done()
        await audit.close()

    inc_counter.assert_called_once_with(
        "rule_allow_audit_dropped_lines_total",
        amount=2,
        attributes={"reason": "shutdown"},
    )
    assert not audit.started
    assert caplog.records == []


@pytest.mark.asyncio
async def test_shutdown_rule_allow_audit_keeps_singleton_after_cancellation() -> None:
    audit = RuleAllowAudit(capacity=1, shutdown_timeout_seconds=1.0)
    rule_allow_audit_module._configured_audit = audit

    close = AsyncMock(side_effect=[asyncio.CancelledError(), None])
    with patch.object(audit, "close", close):
        with pytest.raises(asyncio.CancelledError):
            await shutdown_rule_allow_audit()
        assert rule_allow_audit_module._configured_audit is audit
        await shutdown_rule_allow_audit()

    assert rule_allow_audit_module._configured_audit is None


def test_rule_evaluation_process_metrics_use_rule_and_result_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter_calls: list[tuple[str, int, dict[str, object] | None]] = []
    histogram_calls: list[tuple[str, float, dict[str, object] | None]] = []

    def capture_counter(
        name: str,
        amount: int = 1,
        attributes: dict[str, object] | None = None,
    ) -> None:
        counter_calls.append((name, amount, attributes))

    def capture_histogram(
        name: str,
        value: float,
        attributes: dict[str, object] | None = None,
    ) -> None:
        histogram_calls.append((name, value, attributes))

    monkeypatch.setattr("gobby.telemetry.rule_allow_audit.inc_counter", capture_counter)
    monkeypatch.setattr("gobby.telemetry.rule_allow_audit.observe_histogram", capture_histogram)

    record_rule_evaluation(
        rule_name="block-rule",
        result="block",
        event="before_tool",
        session_id="session-1",
        latency_ms=125.0,
    )

    attributes = {"result": "block", "rule": "block-rule"}
    assert counter_calls == [("rule_evaluations_total", 1, attributes)]
    assert histogram_calls == [("rule_evaluation_duration_seconds", 0.125, attributes)]
