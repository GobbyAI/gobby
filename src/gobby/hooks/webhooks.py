"""Webhook dispatcher for HTTP callouts on hook events.

This module implements config-driven HTTP webhooks that can be triggered
by hook events. It supports:
- Event filtering per endpoint
- Retry with exponential backoff
- Blocking webhooks (can_block) that can deny actions
- Async dispatch for non-blocking webhooks
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from gobby.config.url_validation import validate_endpoint_url
from gobby.hooks.effect_deadline import (
    BLOCKING_EFFECT_BUDGET_SECONDS,
    BlockingEffectDeadline,
    new_blocking_effect_deadline,
    remaining_blocking_effect_seconds,
)
from gobby.hooks.events import HookEvent, HookResponse
from gobby.utils.env import expand_env_mapping, expand_env_variables
from gobby.utils.webhook_transport import WebhookTransport

if TYPE_CHECKING:
    from gobby.config.extensions import WebhookEndpointConfig, WebhooksConfig

logger = logging.getLogger(__name__)
_MAX_WEBHOOK_RESPONSE_BYTES = 64 * 1024


@dataclass
class WebhookResult:
    """Result of a webhook dispatch attempt."""

    endpoint_name: str
    success: bool
    status_code: int | None = None
    response_body: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 1
    duration_ms: float = 0.0
    decision: str | None = None  # For blocking webhooks


class WebhookDispatcher:
    """Dispatches HTTP webhooks on hook events.

    The dispatcher handles:
    - Matching events to configured webhook endpoints
    - HTTP POST requests with JSON payloads
    - Retry logic with exponential backoff
    - Blocking webhooks that can influence hook decisions

    Usage:
        dispatcher = WebhookDispatcher(config)
        results = await dispatcher.trigger(event)

        # For blocking webhooks, check decision
        for result in results:
            if result.decision == "block":
                # Handle blocked action
    """

    def __init__(self, config: WebhooksConfig) -> None:
        """Initialize the webhook dispatcher.

        Args:
            config: Webhooks configuration containing endpoints and settings.
        """
        self.config = config
        self._transport = WebhookTransport(allow_private_addresses=True)
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    def _new_client(self) -> httpx.AsyncClient:
        """Create an HTTP client owned by the caller's event loop."""
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.default_timeout),
            follow_redirects=False,
            trust_env=False,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client.

        Uses double-checked locking to ensure only one client is created
        even when called concurrently from multiple coroutines.
        """
        if self._client is None:
            async with self._client_lock:
                # Double-check after acquiring lock
                if self._client is None:
                    self._client = self._new_client()
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _matches_event(self, endpoint: WebhookEndpointConfig, event_type: str) -> bool:
        """Check if an endpoint should receive the given event type.

        Args:
            endpoint: The webhook endpoint configuration.
            event_type: The hook event type string.

        Returns:
            True if the endpoint should receive this event.
        """
        # Empty events list means all events
        if not endpoint.events:
            return True

        # Normalize event type for comparison (handle both formats)
        # e.g., "session_start" matches "session-start" or "SESSION_START"
        normalized = event_type.lower().replace("-", "_")
        for configured_event in endpoint.events:
            if configured_event.lower().replace("-", "_") == normalized:
                return True

        return False

    def _build_payload(
        self, event: HookEvent, response: HookResponse | None = None
    ) -> dict[str, Any]:
        """Build the webhook payload from a hook event.

        Args:
            event: The hook event to convert to a payload.

        Returns:
            Dictionary payload for the webhook POST body.
        """
        payload = {
            "event_type": event.event_type.value,
            "session_id": event.session_id,
            "source": event.source.value,
            "timestamp": event.timestamp.isoformat(),
            "data": event.data,
            "machine_id": event.machine_id,
            "cwd": event.cwd,
            "project_id": event.project_id,
            "task_id": event.task_id,
            "metadata": event.metadata,
        }
        if response is not None:
            payload["response"] = asdict(response)
        return payload

    async def _dispatch_single(
        self,
        endpoint: WebhookEndpointConfig,
        payload: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> WebhookResult:
        """Dispatch a webhook to a single endpoint with retry logic.

        Args:
            endpoint: The endpoint configuration.
            payload: The JSON payload to send.

        Returns:
            WebhookResult with success/failure info.
        """
        if client is None:
            client = await self._get_client()
        start_time = datetime.now()

        # Build headers
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Gobby-Webhook/1.0",
            "X-Gobby-Event": payload.get("event_type", "unknown"),
        }
        headers.update(expand_env_mapping(endpoint.headers) or {})
        try:
            url = validate_endpoint_url(
                expand_env_variables(endpoint.url),
                field_name="webhook URL",
            )
        except ValueError as exc:
            return WebhookResult(
                endpoint_name=endpoint.name,
                success=False,
                error=f"Invalid webhook URL: {exc}",
                decision="block" if endpoint.can_block and endpoint.fail_closed else None,
            )

        try:
            result = await self._transport.execute(
                url=url,
                method="POST",
                headers=headers,
                payload=payload,
                timeout=endpoint.timeout,
                max_response_bytes=_MAX_WEBHOOK_RESPONSE_BYTES,
                max_attempts=endpoint.retry_count + 1,
                backoff_seconds=endpoint.retry_delay,
                client=client,
            )
        except Exception as exc:
            duration_ms = (datetime.now() - start_time).total_seconds() * 1000
            logger.exception("Webhook %s unexpected error: %s", endpoint.name, exc)
            return WebhookResult(
                endpoint_name=endpoint.name,
                success=False,
                error=str(exc),
                duration_ms=duration_ms,
                decision="block" if endpoint.can_block and endpoint.fail_closed else None,
            )

        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        response_body = result.json_body()
        if result.success:
            logger.debug("Webhook %s succeeded: %s", endpoint.name, result.status_code)
            decision = (
                response_body.get("decision") if endpoint.can_block and response_body else None
            )
        else:
            logger.warning(
                "Webhook %s failed after %s attempt(s): %s",
                endpoint.name,
                result.attempts,
                result.error,
            )
            decision = "block" if endpoint.can_block and endpoint.fail_closed else None

        return WebhookResult(
            endpoint_name=endpoint.name,
            success=result.success,
            status_code=result.status_code,
            response_body=response_body,
            error=result.error,
            attempts=result.attempts,
            duration_ms=duration_ms,
            decision=decision,
        )

    async def _dispatch_blocking(
        self,
        endpoint: WebhookEndpointConfig,
        payload: dict[str, Any],
        deadline: BlockingEffectDeadline,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> WebhookResult:
        """Dispatch one blocking endpoint within the shared hook deadline."""
        remaining = remaining_blocking_effect_seconds(
            deadline,
            maximum=BLOCKING_EFFECT_BUDGET_SECONDS,
        )
        if remaining <= 0:
            return self._blocking_deadline_result(endpoint)
        try:
            async with asyncio.timeout(remaining):
                return await self._dispatch_single(endpoint, payload, client=client)
        except TimeoutError:
            logger.error("Blocking webhook %s exceeded aggregate deadline", endpoint.name)
            return self._blocking_deadline_result(endpoint)

    @staticmethod
    def _blocking_deadline_result(endpoint: WebhookEndpointConfig) -> WebhookResult:
        return WebhookResult(
            endpoint_name=endpoint.name,
            success=False,
            error="Aggregate blocking deadline exceeded",
            decision="block" if endpoint.fail_closed else None,
        )

    async def trigger(
        self,
        event: HookEvent,
        *,
        deadline: BlockingEffectDeadline | None = None,
    ) -> list[WebhookResult]:
        """Trigger webhooks for a hook event.

        Dispatches HTTP POST requests to all matching webhook endpoints.
        Non-blocking webhooks are dispatched concurrently.
        Blocking webhooks (can_block=True) are awaited for their decision.

        Args:
            event: The hook event that triggered this dispatch.

        Returns:
            List of WebhookResult objects for each endpoint triggered.
        """
        if not self.config.enabled:
            return []

        # Find matching endpoints
        event_type = event.event_type.value
        matching_endpoints = [
            ep for ep in self.config.endpoints if ep.enabled and self._matches_event(ep, event_type)
        ]

        if not matching_endpoints:
            return []

        # Build payload once
        payload = self._build_payload(event)

        # Separate blocking and non-blocking webhooks
        blocking = [ep for ep in matching_endpoints if ep.can_block]
        non_blocking = [ep for ep in matching_endpoints if not ep.can_block]

        results: list[WebhookResult] = []
        blocking_deadline = deadline if deadline is not None else new_blocking_effect_deadline()

        # Dispatch blocking webhooks first (sequentially, need their decisions)
        for endpoint in blocking:
            result = await self._dispatch_blocking(endpoint, payload, blocking_deadline)
            results.append(result)

            # If a blocking webhook says "block", we might stop processing
            # But we still dispatch all blocking webhooks to collect all decisions
            if result.decision == "block":
                logger.info("Blocking webhook %s returned decision: block", endpoint.name)

        # Dispatch non-blocking webhooks concurrently
        if non_blocking:
            if self.config.async_dispatch:
                # Fire and forget for truly async dispatch
                tasks = [self._dispatch_single(ep, payload) for ep in non_blocking]
                non_blocking_results = await asyncio.gather(*tasks)
                results.extend(non_blocking_results)
            else:
                # Sequential dispatch
                for endpoint in non_blocking:
                    result = await self._dispatch_single(endpoint, payload)
                    results.append(result)

        return results

    def get_blocking_decision(self, results: list[WebhookResult]) -> tuple[str, str | None]:
        """Get the overall decision from blocking webhook results.

        If any blocking webhook returns "block" or "deny", the overall
        decision is to block the action.

        Args:
            results: List of webhook results from trigger().

        Returns:
            Tuple of (decision, reason) where decision is "allow" or "block".
        """
        for result in results:
            if result.decision in ("block", "deny"):
                reason = None
                if result.response_body:
                    reason = result.response_body.get("reason")
                if reason is None:
                    reason = result.error
                return ("block", reason)

        return ("allow", None)
