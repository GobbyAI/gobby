"""Memory test fixtures."""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator

import pytest


class FakeRedisConnectionError(Exception):
    """Connection error stand-in for tests that fake FalkorDB without redis installed."""


class FakeRedisResponseError(Exception):
    """Response error stand-in for tests that fake FalkorDB without redis installed."""


class FakeRedisTimeoutError(Exception):
    """Timeout error stand-in for tests that fake FalkorDB without redis installed."""


@pytest.fixture(autouse=True)
def fake_redis_exceptions(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide redis.exceptions for Falkor unit tests when the sandbox lacks redis."""
    if importlib.util.find_spec("redis") is not None:
        yield
        return

    fake_redis = types.ModuleType("redis")
    fake_exceptions = types.ModuleType("redis.exceptions")
    fake_exceptions.ConnectionError = FakeRedisConnectionError
    fake_exceptions.ResponseError = FakeRedisResponseError
    fake_exceptions.TimeoutError = FakeRedisTimeoutError
    fake_redis.exceptions = fake_exceptions
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "redis.exceptions", fake_exceptions)
    yield
