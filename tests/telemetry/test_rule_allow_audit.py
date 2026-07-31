"""Focused tests for non-blocking rule allow telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch

import pytest

from gobby.telemetry.rule_allow_audit import RuleAllowAudit, record_rule_evaluation


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
