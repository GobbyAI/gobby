from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from gobby.communications.attachments import AttachmentManager
    from gobby.communications.models import (
        ChannelCapabilities,
        ChannelConfig,
        CommsAttachment,
        CommsMessage,
    )

logger = logging.getLogger(__name__)

_MAX_RETRY_AFTER_DELAY_SECONDS = 120.0


def _clamp_retry_after_delay(delay: float) -> float:
    if not math.isfinite(delay):
        return _MAX_RETRY_AFTER_DELAY_SECONDS
    return min(max(0.0, delay), _MAX_RETRY_AFTER_DELAY_SECONDS)


class BaseChannelAdapter(ABC):
    """Abstract base class for all communication channel adapters."""

    def __init__(self) -> None:
        self._rate_limit_callback: Callable[[float, bool], None] | None = None
        self._inbound_callback: (
            Callable[[list[CommsMessage]], Awaitable[list[CommsMessage]]] | None
        ) = None
        self._config_update_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    @property
    @abstractmethod
    def channel_type(self) -> str:
        """The unique type identifier for this channel (e.g., 'slack', 'discord')."""

    @property
    @abstractmethod
    def max_message_length(self) -> int:
        """Maximum message length supported by the platform."""

    @property
    @abstractmethod
    def supports_webhooks(self) -> bool:
        """Whether this adapter supports inbound webhooks."""

    @property
    @abstractmethod
    def supports_polling(self) -> bool:
        """Whether this adapter supports message polling."""

    @property
    def supports_message_edit(self) -> bool:
        """Whether this adapter can replace an existing platform message."""
        return type(self).edit_message is not BaseChannelAdapter.edit_message

    @property
    def supports_typing(self) -> bool:
        """Whether this adapter can publish a typing indicator."""
        return type(self).send_typing is not BaseChannelAdapter.send_typing

    @property
    def supports_reactions(self) -> bool:
        """Whether this adapter can add or remove message reactions."""
        return type(self).set_reaction is not BaseChannelAdapter.set_reaction

    @abstractmethod
    async def initialize(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        """Set up API clients, validate credentials."""

    @abstractmethod
    async def send_message(self, message: CommsMessage) -> str | None:
        """Send message and return platform message ID."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Cleanly close connections."""

    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""

    @abstractmethod
    def parse_webhook(
        self, payload: dict[str, Any] | bytes, headers: dict[str, str]
    ) -> list[CommsMessage]:
        """Normalize inbound webhook payload."""

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verify webhook signature."""

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send a file attachment and return platform message ID.

        Default raises NotImplementedError. Override in adapters that support files.
        """
        raise NotImplementedError(f"{self.channel_type} adapter does not support file attachments")

    async def edit_message(
        self,
        platform_message_id: str,
        content: str,
        conversation_id: str,
    ) -> None:
        """Replace an existing platform message.

        Override in adapters that support message editing.
        """
        raise NotImplementedError(f"{self.channel_type} adapter does not support message editing")

    async def send_typing(self, conversation_id: str) -> None:
        """Publish a typing indicator for an existing platform conversation.

        Override in adapters that support presence indicators.
        """
        raise NotImplementedError(f"{self.channel_type} adapter does not support typing indicators")

    async def set_reaction(
        self,
        conversation_id: str,
        platform_message_id: str,
        reaction: str | None,
    ) -> None:
        """Add one reaction or remove reactions from an existing message."""
        raise NotImplementedError(f"{self.channel_type} adapter does not support reactions")

    async def download_inbound_attachments(
        self,
        message: CommsMessage,
        attachment_manager: AttachmentManager,
    ) -> list[CommsAttachment]:
        """Download attachments referenced by an inbound message."""
        return []

    async def send_proactive(
        self,
        conversation_id: str,
        content: str,
        content_type: str = "text",
    ) -> str | None:
        """Send a proactive message to an existing platform conversation.

        Default raises NotImplementedError. Override in adapters that support proactive sends.
        """
        raise NotImplementedError(
            f"{self.channel_type} adapter does not support proactive messaging"
        )

    async def poll(self) -> list[CommsMessage]:
        """Poll for new messages (default implementation returns empty list)."""
        return []

    async def acknowledge_messages(self, messages: list[CommsMessage]) -> None:
        """Acknowledge successfully handled polled messages."""
        return None

    def platform_destination(self, message: CommsMessage) -> str:
        """Return the adapter-facing destination for an outbound message.

        ``message.channel_id`` is the internal comms channel UUID. Adapters must
        use this helper to route via ``metadata_json["platform_destination"]``.
        """
        destination = message.metadata_json.get("platform_destination")
        if destination is None or destination == "":
            raise ValueError(
                f"No platform_destination provided in message metadata for {self.channel_type}"
            )
        return str(destination)

    def set_rate_limit_callback(self, callback: Callable[[float, bool], None]) -> None:
        """Set a callback invoked when an adapter detects a platform rate limit.

        Args:
            callback: Callable(duration_seconds, is_global). The manager uses this
                      to propagate backoff to the TokenBucketRateLimiter.
        """
        self._rate_limit_callback = callback

    def set_inbound_callback(
        self,
        callback: Callable[[list[CommsMessage]], Awaitable[list[CommsMessage]]],
    ) -> None:
        """Set a callback invoked when an adapter receives inbound messages directly."""
        self._inbound_callback = callback

    def set_config_update_callback(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Set a callback that persists adapter-owned channel configuration."""
        self._config_update_callback = callback

    async def _update_channel_config(self, values: dict[str, Any]) -> bool:
        """Persist channel configuration values through the owning manager."""
        if self._config_update_callback is None:
            return False
        await self._config_update_callback(values)
        return True

    async def _handle_inbound_messages(self, messages: list[CommsMessage]) -> list[CommsMessage]:
        """Forward adapter-received inbound messages to the manager."""
        if self._inbound_callback is None:
            logger.debug("%s inbound callback is not configured", self.channel_type)
            return []
        return await self._inbound_callback(messages)

    async def _retry(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> Any:
        """Retry an arbitrary async callable with exponential backoff.

        Unlike _retry_request (HTTP-specific), this retries any async operation
        on exception. Used for SMTP/IMAP reconnects, etc.
        """
        max_retries = max(0, max_retries)
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await coro_factory()
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = backoff_base * (2**attempt)
                    logger.warning(
                        "%s operation failed, retrying in %.1fs (attempt %d/%d): %s",
                        self.channel_type,
                        delay,
                        attempt + 1,
                        max_retries + 1,
                        exc,
                    )
                    await asyncio.sleep(delay)
        if last_exc is None:
            raise RuntimeError("Retry loop completed without producing a result.")
        raise last_exc

    async def _retry_request(
        self,
        coro_factory: Callable[[], Awaitable[httpx.Response]],
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> httpx.Response:
        """Execute an HTTP request with retry logic for 429 and 5xx responses.

        Args:
            coro_factory: Zero-arg callable that returns a new awaitable for each attempt.
            max_retries: Maximum number of retry attempts.
            backoff_base: Base delay in seconds for exponential backoff.

        Returns:
            The successful HTTP response.

        Raises:
            httpx.HTTPStatusError: If all retries are exhausted or a non-retryable error occurs.
        """
        max_retries = max(0, max_retries)
        last_response: httpx.Response | None = None
        for attempt in range(max_retries + 1):
            response = await coro_factory()
            last_response = response

            if response.status_code == 429:
                if attempt >= max_retries:
                    break
                retry_after = response.headers.get("Retry-After")
                delay = backoff_base * (2**attempt)  # default fallback
                if retry_after:
                    try:
                        delay = _clamp_retry_after_delay(float(retry_after))
                    except ValueError:
                        try:
                            dt = parsedate_to_datetime(retry_after)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=UTC)
                            delay = _clamp_retry_after_delay(
                                (dt - datetime.now(UTC)).total_seconds()
                            )
                        except (ValueError, TypeError):
                            pass  # keep exponential backoff default
                if self._rate_limit_callback is not None:
                    self._rate_limit_callback(delay, False)
                logger.warning(
                    "%s rate limited (429), retrying in %.1fs (attempt %d/%d)",
                    self.channel_type,
                    delay,
                    attempt + 1,
                    max_retries + 1,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 500 and attempt < max_retries:
                delay = backoff_base * (2**attempt)
                logger.warning(
                    "%s server error %d, retrying in %.1fs (attempt %d/%d)",
                    self.channel_type,
                    response.status_code,
                    delay,
                    attempt + 1,
                    max_retries + 1,
                )
                await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            return response

        # All retries exhausted — raise on last response
        if last_response is None:
            raise RuntimeError("Retry loop completed without producing a response.")
        last_response.raise_for_status()
        return last_response  # Unreachable, but satisfies type checker

    def chunk_message(self, content: str, max_length: int | None = None) -> list[str]:
        """Split long messages respecting word boundaries."""
        limit = max_length or self.max_message_length
        if limit < 1:
            raise ValueError("max_length must be positive")
        if len(content) <= limit:
            return [content]

        chunks = []
        remaining = content
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break

            # Check if we can split exactly at limit (next char is space)
            if remaining[limit] == " ":
                split_idx = limit
            else:
                # Find last space within limit
                split_idx = remaining.rfind(" ", 0, limit)
                if split_idx == -1:
                    # No space found, hard split
                    split_idx = limit

            chunk = remaining[:split_idx].rstrip()
            if not chunk:
                chunk = remaining[:limit]
                remaining = remaining[limit:]
            else:
                remaining = remaining[split_idx:].lstrip()
            chunks.append(chunk)

        return chunks


# Status update trigger
