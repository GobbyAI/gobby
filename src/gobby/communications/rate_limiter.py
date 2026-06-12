"""Token-bucket rate limiter for communication channels."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from gobby.config.communications import ChannelDefaults


class RateLimitWaitExceeded(TimeoutError):
    """Raised when waiting for a rate-limit token exceeds the configured bound."""


@dataclass
class _Bucket:
    """Internal state for a single token bucket."""

    tokens: float
    last_refill: float
    rate: float  # tokens per second
    burst: int


class TokenBucketRateLimiter:
    """Token-bucket rate limiter with per-channel tracking.

    Note: This is an in-memory, single-instance rate limiter. It is suitable for
    Gobby's single-daemon architecture but would not work for a multi-instance deployment.
    """

    def __init__(
        self,
        default_rate: int = 30,
        default_burst: int = 5,
        max_wait_seconds: float = 60.0,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            default_rate: Default tokens per minute.
            default_burst: Default maximum tokens (burst size).
            max_wait_seconds: Maximum time to wait for a token.
        """
        self._validate_limits(default_rate, default_burst)
        if max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be greater than 0")
        self._default_rate = default_rate / 60.0
        self._default_burst = default_burst
        self._max_wait_seconds = max_wait_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._backoffs: dict[str, float] = {}  # channel_id -> monotonic expiry time

    @staticmethod
    def _validate_limits(rate: int, burst: int) -> None:
        if rate < 1:
            raise ValueError("rate must be greater than or equal to 1")
        if burst < 1:
            raise ValueError("burst must be greater than or equal to 1")

    @classmethod
    def from_defaults(cls, defaults: ChannelDefaults) -> TokenBucketRateLimiter:
        """Create a rate limiter from ChannelDefaults."""
        return cls(
            default_rate=defaults.rate_limit_per_minute,
            default_burst=defaults.burst,
        )

    def set_backoff(self, channel_id: str, duration_seconds: float) -> None:
        """Set a mandatory backoff for a channel.

        Args:
            channel_id: The ID of the channel.
            duration_seconds: How long to back off in seconds.
        """
        now = time.monotonic()
        # Prune expired entries to prevent unbounded growth
        expired = [k for k, v in self._backoffs.items() if v <= now]
        for k in expired:
            del self._backoffs[k]
        expiry = now + duration_seconds
        self._backoffs[channel_id] = max(self._backoffs.get(channel_id, 0), expiry)

    def _get_or_create_bucket(self, channel_id: str) -> _Bucket:
        """Get an existing bucket or create a new one with default settings."""
        if channel_id not in self._buckets:
            self._buckets[channel_id] = _Bucket(
                tokens=float(self._default_burst),
                last_refill=time.monotonic(),
                rate=self._default_rate,
                burst=self._default_burst,
            )
        return self._buckets[channel_id]

    def configure_channel(self, channel_id: str, rate: int, burst: int) -> None:
        """Configure specific limits for a channel.

        Args:
            channel_id: The ID of the channel.
            rate: Tokens per minute for this channel.
            burst: Maximum tokens (burst size) for this channel.
        """
        self._validate_limits(rate, burst)
        r = rate / 60.0
        if channel_id in self._buckets:
            bucket = self._buckets[channel_id]
            bucket.rate = r
            bucket.burst = burst
            # Cap tokens if new burst is smaller
            bucket.tokens = min(bucket.tokens, float(burst))
        else:
            self._buckets[channel_id] = _Bucket(
                tokens=float(burst),
                last_refill=time.monotonic(),
                rate=r,
                burst=burst,
            )

    async def _sleep_with_deadline(
        self, channel_id: str, wait_time: float, deadline: float
    ) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or wait_time > remaining:
            raise RateLimitWaitExceeded(
                f"Rate limit wait exceeded {self._max_wait_seconds:g}s for channel {channel_id!r}"
            )
        await asyncio.sleep(wait_time)

    def _refill(self, bucket: _Bucket) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - bucket.last_refill
        if elapsed > 0:
            new_tokens = elapsed * bucket.rate
            bucket.tokens = min(float(bucket.burst), bucket.tokens + new_tokens)
            bucket.last_refill = now

    def check(self, channel_id: str) -> bool:
        """Check if a token is available and consume it if so.

        This follows the token-bucket algorithm, refilling tokens based on elapsed time
        since the last check or refill operation.

        Args:
            channel_id: The ID of the channel to check.

        Returns:
            True if a token was consumed, False otherwise.
        """
        # Check backoff first
        if time.monotonic() < self._backoffs.get(channel_id, 0):
            return False

        bucket = self._get_or_create_bucket(channel_id)
        self._refill(bucket)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False

    async def wait_if_needed(self, channel_id: str) -> None:
        """Wait until a token is available for the channel.

        Args:
            channel_id: The ID of the channel.
        """
        deadline = time.monotonic() + self._max_wait_seconds
        while True:
            # Respect backoff
            now = time.monotonic()
            backoff_expiry = self._backoffs.get(channel_id, 0)
            if now < backoff_expiry:
                await self._sleep_with_deadline(channel_id, backoff_expiry - now, deadline)
                continue

            # We don't really need the lock for single bucket ops as they are atomic-ish in GIL,
            # but for refill + check it's safer. However, this is local memory state.
            bucket = self._get_or_create_bucket(channel_id)
            self._refill(bucket)

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return

            # Calculate wait time
            needed = 1.0 - bucket.tokens
            wait_time = needed / bucket.rate

            # Sleep to allow other tasks to proceed
            await self._sleep_with_deadline(channel_id, wait_time, deadline)

    def reset(self, channel_id: str) -> None:
        """Reset a channel's bucket to full.

        Args:
            channel_id: The ID of the channel.
        """
        if channel_id in self._buckets:
            bucket = self._buckets[channel_id]
            bucket.tokens = float(bucket.burst)
            bucket.last_refill = time.monotonic()

    def remove_channel(self, channel_id: str) -> None:
        """Remove a channel's bucket configuration.

        Args:
            channel_id: The ID of the channel.
        """
        self._buckets.pop(channel_id, None)
