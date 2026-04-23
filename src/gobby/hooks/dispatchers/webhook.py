"""Webhook evaluation and dispatch functions.

Extracted from HookManager — these functions handle blocking and non-blocking
webhook dispatch for hook events.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.logging_utils import block_tool_name_from_event_data, log_structured_block
from gobby.hooks.webhooks import WebhookDispatcher, WebhookResult


def _resolve_webhook_block_reason(
    event: HookEvent,
    reason: str | None,
    logger: logging.Logger,
) -> str:
    """Return a non-empty webhook block reason and warn on fallback use."""
    cleaned = (reason or "").strip()
    if cleaned:
        return cleaned
    logger.warning(
        "BLOCK fallback session=%s event=%s tool=%s source=webhook "
        "rule=webhook-dispatch detail=blocking webhook omitted reason",
        event.session_id,
        event.event_type.value,
        block_tool_name_from_event_data(event.data),
    )
    return (
        f"Blocking webhook denied {event.event_type.value} without providing a reason. "
        "Inspect webhook responses for the blocking endpoint."
    )


def evaluate_blocking_webhooks(
    event: HookEvent,
    webhook_dispatcher: WebhookDispatcher,
    logger: logging.Logger,
    loop: asyncio.AbstractEventLoop | None,
) -> HookResponse | None:
    """Evaluate blocking webhooks before handler execution.

    Args:
        event: The hook event to evaluate webhooks for.
        webhook_dispatcher: The WebhookDispatcher instance.
        logger: Logger for diagnostics.
        loop: Captured event loop for thread-safe scheduling.

    Returns:
        HookResponse if a webhook blocked the event, None otherwise.
    """
    try:
        webhook_results = dispatch_webhooks_sync(
            event,
            webhook_dispatcher,
            logger,
            blocking_only=True,
        )
        decision, reason = webhook_dispatcher.get_blocking_decision(webhook_results)
        if decision == "block":
            resolved_reason = _resolve_webhook_block_reason(event, reason, logger)
            log_structured_block(
                logger,
                session_id=event.session_id,
                event=event.event_type.value,
                tool=block_tool_name_from_event_data(event.data),
                source="webhook",
                rule="webhook-dispatch",
                reason=resolved_reason,
            )
            return HookResponse(decision="block", reason=resolved_reason)
    except Exception as e:
        logger.error(f"Blocking webhook dispatch failed: {e}", exc_info=True)
        # Fail-open for webhook errors
    return None


def dispatch_webhooks_sync(
    event: HookEvent,
    webhook_dispatcher: WebhookDispatcher,
    logger: logging.Logger,
    blocking_only: bool = False,
) -> list[WebhookResult]:
    """Dispatch webhooks synchronously (for blocking webhooks).

    Args:
        event: The hook event to dispatch.
        webhook_dispatcher: The WebhookDispatcher instance.
        logger: Logger for diagnostics.
        blocking_only: If True, only dispatch to blocking (can_block=True) endpoints.

    Returns:
        List of WebhookResult objects.
    """
    if not webhook_dispatcher.config.enabled:
        return []

    # Filter endpoints if blocking_only
    matching_endpoints = [
        ep
        for ep in webhook_dispatcher.config.endpoints
        if ep.enabled
        and webhook_dispatcher._matches_event(ep, event.event_type.value)
        and (not blocking_only or ep.can_block)
    ]

    if not matching_endpoints:
        return []

    # Build payload once
    payload = webhook_dispatcher._build_payload(event)

    # Run async dispatch in sync context
    async def dispatch_all() -> list[WebhookResult]:
        results: list[WebhookResult] = []
        for endpoint in matching_endpoints:
            result = await webhook_dispatcher._dispatch_single(endpoint, payload)
            results.append(result)
        return results

    # Execute in event loop
    try:
        asyncio.get_running_loop()
        # Already in async context - this method shouldn't be called here
        # Fall back to creating a new thread to run the coroutine synchronously
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, dispatch_all())
            return future.result()
    except RuntimeError:
        # Not in async context, run synchronously
        return asyncio.run(dispatch_all())


def dispatch_webhooks_async(
    event: HookEvent,
    webhook_dispatcher: WebhookDispatcher,
    logger: logging.Logger,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Dispatch non-blocking webhooks asynchronously (fire-and-forget).

    Args:
        event: The hook event to dispatch.
        webhook_dispatcher: The WebhookDispatcher instance.
        logger: Logger for diagnostics.
        loop: Captured event loop for thread-safe scheduling.
    """
    if not webhook_dispatcher.config.enabled:
        return

    # Filter to non-blocking endpoints only
    matching_endpoints = [
        ep
        for ep in webhook_dispatcher.config.endpoints
        if ep.enabled
        and webhook_dispatcher._matches_event(ep, event.event_type.value)
        and not ep.can_block
    ]

    if not matching_endpoints:
        return

    # Build payload
    payload = webhook_dispatcher._build_payload(event)

    async def dispatch_all() -> None:
        tasks = [webhook_dispatcher._dispatch_single(ep, payload) for ep in matching_endpoints]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ep, result in zip(matching_endpoints, results, strict=True):
            if isinstance(result, Exception):
                logger.warning(f"Webhook dispatch to {ep.url} failed: {result}")

    # Fire and forget
    try:
        running_loop = asyncio.get_running_loop()
        running_loop.create_task(dispatch_all())
    except RuntimeError:
        # No event loop, try using captured loop
        if loop:
            try:
                asyncio.run_coroutine_threadsafe(dispatch_all(), loop)
            except Exception as e:
                logger.warning(f"Failed to schedule async webhook: {e}")
