"""Non-blocking process metrics and durable audit delivery for rule allows."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from gobby.config.logging import LoggingSettings
from gobby.telemetry.instruments import inc_counter, observe_histogram

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("gobby.rule_allow_audit")

_WRITE_WARNING_INTERVAL_SECONDS = 60.0

AuditWriter = Callable[[str], Awaitable[None]]
RuleResult = Literal["allow", "block"]


class RuleAllowAudit:
    """Bounded drop-newest queue backed by one asynchronous writer task."""

    def __init__(
        self,
        *,
        capacity: int,
        shutdown_timeout_seconds: float,
        writer: AuditWriter | None = None,
    ) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=capacity)
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._writer = writer or self._write_line
        self._writer_task: asyncio.Task[None] | None = None
        self._accepting = True
        self._in_flight = False
        self._last_write_warning_at: float | None = None

    @property
    def started(self) -> bool:
        return self._writer_task is not None

    def record(
        self,
        *,
        rule_name: str,
        event: str,
        session_id: str,
        latency_ms: float,
    ) -> bool:
        """Queue one JSON line without awaiting or performing file I/O."""
        if not self._accepting:
            inc_counter(
                "rule_allow_audit_dropped_lines_total",
                attributes={"reason": "shutdown"},
            )
            return False

        line = json.dumps(
            {
                "event": event,
                "latency_ms": latency_ms,
                "result": "allow",
                "rule_name": rule_name,
                "session_id": session_id,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            inc_counter(
                "rule_allow_audit_dropped_lines_total",
                attributes={"reason": "overflow"},
            )
            return False

        if self._writer_task is None:
            self._writer_task = asyncio.get_running_loop().create_task(
                self._run_writer(),
                name="rule-allow-audit-writer",
            )
        return True

    async def close(self) -> None:
        """Drain accepted lines within the configured hard deadline."""
        self._accepting = False
        task = self._writer_task
        if task is None:
            return

        try:
            await asyncio.wait_for(self._queue.join(), timeout=self._shutdown_timeout_seconds)
        except TimeoutError:
            residual = self._queue.qsize() + int(self._in_flight)
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            if residual:
                inc_counter(
                    "rule_allow_audit_dropped_lines_total",
                    amount=residual,
                    attributes={"reason": "shutdown"},
                )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._writer_task = None

    async def _run_writer(self) -> None:
        while True:
            line = await self._queue.get()
            self._in_flight = True
            try:
                await self._writer(line)
            except Exception as exc:
                inc_counter("rule_allow_audit_write_errors_total")
                now = asyncio.get_running_loop().time()
                if (
                    self._last_write_warning_at is None
                    or now - self._last_write_warning_at >= _WRITE_WARNING_INTERVAL_SECONDS
                ):
                    self._last_write_warning_at = now
                    logger.warning("Failed to write rule allow audit line: %s", exc)
            finally:
                self._in_flight = False
                self._queue.task_done()

    @staticmethod
    async def _write_line(line: str) -> None:
        await asyncio.to_thread(_audit_logger.info, line)


_configured_audit: RuleAllowAudit | None = None


def configure_rule_allow_audit(config: LoggingSettings) -> None:
    """Configure the process singleton before rule evaluation starts."""
    global _configured_audit
    if _configured_audit is not None and _configured_audit.started:
        logger.warning("Ignored rule allow audit reconfiguration while writer is active")
        return
    _configured_audit = RuleAllowAudit(
        capacity=config.allow_audit_queue_capacity,
        shutdown_timeout_seconds=config.allow_audit_shutdown_timeout_seconds,
    )


def record_rule_evaluation(
    *,
    rule_name: str,
    result: RuleResult,
    event: str,
    session_id: str,
    latency_ms: float,
) -> None:
    """Record process metrics and queue the durable allow record when applicable."""
    attributes = {"result": result, "rule": rule_name}
    inc_counter("rule_evaluations_total", attributes=attributes)
    observe_histogram(
        "rule_evaluation_duration_seconds",
        latency_ms / 1000,
        attributes=attributes,
    )
    if result == "allow" and _configured_audit is not None:
        _configured_audit.record(
            rule_name=rule_name,
            event=event,
            session_id=session_id,
            latency_ms=latency_ms,
        )


async def shutdown_rule_allow_audit() -> None:
    """Stop accepting records and drain the configured writer."""
    global _configured_audit
    audit = _configured_audit
    _configured_audit = None
    if audit is not None:
        await audit.close()
