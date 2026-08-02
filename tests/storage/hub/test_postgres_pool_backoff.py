from __future__ import annotations

import importlib
import logging
import time
from types import ModuleType
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool, PoolTimeout

from gobby.config.postgres_pool import PostgresPoolConfig, postgres_pool_config_from_mapping
from gobby.storage.hub import postgres_pool

pytestmark = pytest.mark.unit


class _TimeoutContext:
    def __enter__(self) -> Any:
        raise PoolTimeout("couldn't get a connection after 5.00 sec")

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _ConnectionContext:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def __enter__(self) -> Any:
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakePool:
    def __init__(self, failures: int) -> None:
        self.failures_remaining = failures
        self.connection_calls = 0
        self.check_calls = 0

    def connection(self) -> Any:
        self.connection_calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            return _TimeoutContext()
        return _ConnectionContext(object())

    def check(self) -> None:
        self.check_calls += 1


def _pool_stats() -> dict[str, int]:
    return {"pool_size": 20, "pool_available": 0}


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


def test_pool_connection_recovers_after_backoff_retries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _FakePool(failures=3)
    sleeps = _patch_sleep(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=postgres_pool.__name__):
        with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats) as conn:
            assert conn is not None

    assert pool.connection_calls == 4
    assert pool.check_calls == 3
    assert len(sleeps) == 3
    for sleep, base in zip(sleeps, postgres_pool.POOL_TIMEOUT_RETRY_BACKOFF_SECONDS, strict=True):
        assert base <= sleep <= base * (1 + postgres_pool.POOL_TIMEOUT_RETRY_JITTER_RATIO)
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []


def test_pool_connection_logs_single_error_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _FakePool(failures=99)
    sleeps = _patch_sleep(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=postgres_pool.__name__):
        with pytest.raises(PoolTimeout):
            with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats):
                pass

    attempts = len(postgres_pool.POOL_TIMEOUT_RETRY_BACKOFF_SECONDS)
    assert pool.connection_calls == attempts + 1
    assert pool.check_calls == attempts
    assert len(sleeps) == attempts

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert "failed after" in error_records[0].getMessage()
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records == []


def test_pool_connection_checks_pool_before_each_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class OrderedPool:
        def __init__(self) -> None:
            self.failures = 1

        def connection(self) -> Any:
            events.append("connection")
            if self.failures > 0:
                self.failures -= 1
                return _TimeoutContext()
            return _ConnectionContext(object())

        def check(self) -> None:
            events.append("check")

    pool = OrderedPool()
    _patch_sleep(monkeypatch)

    with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats):
        pass

    assert events == ["connection", "check", "connection"]


def _postgres_module() -> ModuleType:
    return importlib.import_module("gobby.storage.hub.postgres")


def test_hub_pool_uses_max_lifetime_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _postgres_module()
    calls: dict[str, Any] = {}

    class FakePool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.update(kwargs)

        def open(self, *, wait: bool, timeout: float) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "ConnectionPool", FakePool)
    module.PostgresHubDatabase(
        "postgresql://gobby:secret@localhost/gobby",
        pool_config=PostgresPoolConfig(max_lifetime_seconds=120.0),
    )

    assert calls["max_lifetime"] == 120.0


def test_postgres_pool_config_defaults_max_lifetime() -> None:
    config = PostgresPoolConfig()
    assert config.max_lifetime_seconds == 300.0
    assert config.to_dict()["max_lifetime_seconds"] == 300.0


def test_postgres_pool_config_from_mapping_reads_max_lifetime() -> None:
    config = postgres_pool_config_from_mapping({"max_lifetime_seconds": 600})
    assert config.max_lifetime_seconds == 600.0


def test_postgres_pool_config_rejects_invalid_max_lifetime() -> None:
    with pytest.raises(ValueError, match="max_lifetime_seconds"):
        PostgresPoolConfig(max_lifetime_seconds=0)
    with pytest.raises(ValueError, match="max_lifetime_seconds"):
        postgres_pool_config_from_mapping({"max_lifetime_seconds": "five"})
