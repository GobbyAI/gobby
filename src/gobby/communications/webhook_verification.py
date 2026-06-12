"""Async webhook verification helpers."""

from __future__ import annotations

import asyncio

from gobby.communications.adapters.base import BaseChannelAdapter


async def verify_webhook_with_timeout(
    adapter: BaseChannelAdapter,
    payload: bytes,
    headers: dict[str, str],
    secret: str,
    timeout_seconds: float,
) -> bool:
    """Run synchronous adapter webhook verification off the event loop."""
    return await asyncio.wait_for(
        asyncio.to_thread(adapter.verify_webhook, payload, headers, secret),
        timeout=timeout_seconds,
    )
