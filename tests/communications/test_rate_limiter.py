"""Tests for TokenBucketRateLimiter."""

from __future__ import annotations

import time

import pytest

from gobby.communications.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_check():
    # 60 tokens per min = 1 token per sec. Burst = 2.
    limiter = TokenBucketRateLimiter(default_rate=60, default_burst=2)

    # First check consumes one token
    assert limiter.check("chan-1") is True
    # Second check consumes the second burst token
    assert limiter.check("chan-1") is True
    # Third check fails (empty)
    assert limiter.check("chan-1") is False


@pytest.mark.asyncio
async def test_rate_limiter_refill():
    # 600 tokens per min = 10 tokens per sec.
    limiter = TokenBucketRateLimiter(default_rate=600, default_burst=1)

    assert limiter.check("chan-1") is True
    assert limiter.check("chan-1") is False

    limiter._buckets["chan-1"].last_refill -= 0.15
    assert limiter.check("chan-1") is True


@pytest.mark.asyncio
async def test_rate_limiter_wait():
    # 60 tokens per min = 1 token per sec.
    limiter = TokenBucketRateLimiter(default_rate=60, default_burst=1)

    # Consume initial token
    assert limiter.check("chan-1") is True

    start = time.monotonic()
    # Should wait ~1s
    await limiter.wait_if_needed("chan-1")
    end = time.monotonic()

    assert end - start >= 0.9


@pytest.mark.asyncio
async def test_rate_limiter_configure():
    limiter = TokenBucketRateLimiter(default_rate=1, default_burst=1)

    # Configure channel with high rate
    limiter.configure_channel("fast-chan", rate=6000, burst=10)

    for _ in range(10):
        assert limiter.check("fast-chan") is True
    assert limiter.check("fast-chan") is False


@pytest.mark.asyncio
async def test_rate_limiter_reset_remove():
    limiter = TokenBucketRateLimiter(default_rate=60, default_burst=1)

    assert limiter.check("chan-1") is True
    assert limiter.check("chan-1") is False

    limiter.reset("chan-1")
    assert limiter.check("chan-1") is True

    limiter.remove_channel("chan-1")
    # Should re-create with defaults
    assert limiter.check("chan-1") is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rate_limiter_backoff():
    limiter = TokenBucketRateLimiter(default_rate=60, default_burst=10)

    # Normal check passes
    assert limiter.check("chan-1") is True

    # Set 0.2s backoff
    limiter.set_backoff("chan-1", 0.2)

    # Should fail immediately even if tokens are available
    assert limiter.check("chan-1") is False

    # wait_if_needed should wait
    start = time.monotonic()
    await limiter.wait_if_needed("chan-1")
    end = time.monotonic()

    assert end - start >= 0.15
    # Should pass now
    assert limiter.check("chan-1") is True
