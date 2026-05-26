"""Daemon readiness gate for hook execution."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.shutdown_intent import ShutdownIntent, read_active_shutdown_intent

CRITICAL_HOOKS = {
    HookEventType.SESSION_START,
    HookEventType.SESSION_END,
    HookEventType.PRE_COMPACT,
    HookEventType.AFTER_AGENT,
    HookEventType.STOP,
}
RETRY_DELAYS = (0.5, 1.0, 2.0)
PLANNED_RESTART_MARKER_MAX_AGE_SECONDS = 120.0


class HealthMonitorProtocol(Protocol):
    def get_cached_status(self) -> tuple[bool, str | None, str, str | None]: ...

    def check_now(self) -> bool: ...


def _unavailable_response(
    event: HookEvent,
    daemon_status: str,
    error_reason: str | None,
    logger: logging.Logger,
) -> HookResponse:
    restart_source = _planned_restart_source()
    if restart_source:
        logger.debug(
            "Daemon unavailable during planned restart, skipping hook execution: %s. "
            "Status: %s, Error: %s, Source: %s",
            event.event_type,
            daemon_status,
            error_reason,
            restart_source,
        )
        return HookResponse(
            decision="allow",
            reason=f"Daemon restarting ({restart_source}): {error_reason or 'Unknown'}",
        )

    logger.warning(
        "Daemon not available after retries, skipping hook execution: %s. Status: %s, Error: %s",
        event.event_type,
        daemon_status,
        error_reason,
    )
    return HookResponse(
        decision="allow",
        reason=f"Daemon {daemon_status}: {error_reason or 'Unknown'}",
    )


def _planned_restart_source() -> str | None:
    record = read_active_shutdown_intent(max_age_seconds=PLANNED_RESTART_MARKER_MAX_AGE_SECONDS)
    if record is None or record.stale or record.error:
        return None
    if record.intent is not ShutdownIntent.RESTART:
        return None
    return record.source


def ensure_daemon_ready(
    event: HookEvent,
    health_monitor: HealthMonitorProtocol,
    logger: logging.Logger,
) -> HookResponse | None:
    """Return fail-open response when daemon is unavailable."""
    is_ready, _, daemon_status, error_reason = health_monitor.get_cached_status()

    if not is_ready and event.event_type in CRITICAL_HOOKS:
        for attempt, delay in enumerate(RETRY_DELAYS, 1):
            time.sleep(delay)
            is_ready = health_monitor.check_now()
            if is_ready:
                logger.info(
                    "Daemon recovered after %d retry(ies) for %s",
                    attempt,
                    event.event_type,
                )
                break
            logger.debug(
                "Daemon still unavailable, retry %d/%d for %s",
                attempt,
                len(RETRY_DELAYS),
                event.event_type,
            )

    if is_ready:
        return None

    return _unavailable_response(event, daemon_status, error_reason, logger)


async def ensure_daemon_ready_async(
    event: HookEvent,
    health_monitor: HealthMonitorProtocol,
    logger: logging.Logger,
) -> HookResponse | None:
    """Async daemon readiness gate for event-loop hook callers."""
    is_ready, _, daemon_status, error_reason = health_monitor.get_cached_status()

    if not is_ready and event.event_type in CRITICAL_HOOKS:
        for attempt, delay in enumerate(RETRY_DELAYS, 1):
            await asyncio.sleep(delay)
            is_ready = await asyncio.to_thread(health_monitor.check_now)
            if is_ready:
                logger.info(
                    "Daemon recovered after %d retry(ies) for %s",
                    attempt,
                    event.event_type,
                )
                break
            logger.debug(
                "Daemon still unavailable, retry %d/%d for %s",
                attempt,
                len(RETRY_DELAYS),
                event.event_type,
            )

    if is_ready:
        return None

    return _unavailable_response(event, daemon_status, error_reason, logger)
