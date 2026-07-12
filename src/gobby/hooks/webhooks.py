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
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from gobby.utils.env import expand_env_mapping, expand_env_variables

if TYPE_CHECKING:
    from gobby.config.extensions import WebhookEndpointConfig, WebhooksConfig
    from gobby.hooks.events import HookEvent

logger = logging.getLogger(__name__)


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
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client.

        Uses double-checked locking to ensure only one client is created
        even when called concurrently from multiple coroutines.
        """
        if self._client is None:
            async with self._client_lock:
                # Double-check after acquiring lock
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(self.config.default_timeout),
                        follow_redirects=True,
                    )
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

    def _build_payload(self, event: HookEvent) -> dict[str, Any]:
        """Build the webhook payload from a hook event.

        Args:
            event: The hook event to convert to a payload.

        Returns:
            Dictionary payload for the webhook POST body.
        """
        return {
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

    async def _dispatch_single(
        self,
        endpoint: WebhookEndpointConfig,
        payload: dict[str, Any],
    ) -> WebhookResult:
        """Dispatch a webhook to a single endpoint with retry logic.

        Args:
            endpoint: The endpoint configuration.
            payload: The JSON payload to send.

        Returns:
            WebhookResult with success/failure info.
        """
        client = await self._get_client()
        start_time = datetime.now()
        attempts = 0
        last_error: str | None = None
        delay = endpoint.retry_delay

        # Build headers
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Gobby-Webhook/1.0",
            "X-Gobby-Event": payload.get("event_type", "unknown"),
        }
        headers.update(expand_env_mapping(endpoint.headers) or {})
        url = expand_env_variables(endpoint.url)

        while attempts <= endpoint.retry_count:
            attempts += 1

            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=endpoint.timeout,
                )

                duration_ms = (datetime.now() - start_time).total_seconds() * 1000

                # Parse response body if JSON
                response_body: dict[str, Any] | None = None
                try:
                    response_body = response.json()
                except (json.JSONDecodeError, ValueError):
                    pass

                # Check if blocking webhook and extract decision
                decision: str | None = None
                if endpoint.can_block and response_body:
                    decision = response_body.get("decision")

                # Success on 2xx status codes
                if 200 <= response.status_code < 300:
                    logger.debug(f"Webhook {endpoint.name} succeeded: {response.status_code}")
                    return WebhookResult(
                        endpoint_name=endpoint.name,
                        success=True,
                        status_code=response.status_code,
                        response_body=response_body,
                        attempts=attempts,
                        duration_ms=duration_ms,
                        decision=decision,
                    )

                # 4xx errors are not retryable (client error)
                if 400 <= response.status_code < 500:
                    logger.warning(f"Webhook {endpoint.name} client error: {response.status_code}")
                    return WebhookResult(
                        endpoint_name=endpoint.name,
                        success=False,
                        status_code=response.status_code,
                        response_body=response_body,
                        error=f"HTTP {response.status_code}",
                        attempts=attempts,
                        duration_ms=duration_ms,
                        decision="block" if endpoint.can_block and endpoint.fail_closed else None,
                    )

                # 5xx errors are retryable
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    f"Webhook {endpoint.name} server error: {response.status_code}, "
                    f"attempt {attempts}/{endpoint.retry_count + 1}"
                )

            except httpx.TimeoutException:
                last_error = "Request timeout"
                logger.warning(
                    f"Webhook {endpoint.name} timeout, "
                    f"attempt {attempts}/{endpoint.retry_count + 1}"
                )

            except httpx.ConnectError as e:
                last_error = f"Connection error: {e}"
                logger.warning(
                    f"Webhook {endpoint.name} connection error: {e}, "
                    f"attempt {attempts}/{endpoint.retry_count + 1}"
                )

            except Exception as e:
                last_error = str(e)
                logger.exception(f"Webhook {endpoint.name} unexpected error: {e}")

            # Wait before retry with exponential backoff
            if attempts <= endpoint.retry_count:
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff

        # All retries exhausted
        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        logger.error(f"Webhook {endpoint.name} failed after {attempts} attempts: {last_error}")

        return WebhookResult(
            endpoint_name=endpoint.name,
            success=False,
            error=last_error,
            attempts=attempts,
            duration_ms=duration_ms,
            decision="block" if endpoint.can_block and endpoint.fail_closed else None,
        )

    async def trigger(self, event: HookEvent) -> list[WebhookResult]:
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

        # Dispatch blocking webhooks first (sequentially, need their decisions)
        for endpoint in blocking:
            result = await self._dispatch_single(endpoint, payload)
            results.append(result)

            # If a blocking webhook says "block", we might stop processing
            # But we still dispatch all blocking webhooks to collect all decisions
            if result.decision == "block":
                logger.info(f"Blocking webhook {endpoint.name} returned decision: block")

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
