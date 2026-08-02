from __future__ import annotations

import logging

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.utils.logging import ThrottledLogger

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "exc",
    [
        PoolTimeout("couldn't get a connection after 5.00 sec"),
        psycopg.OperationalError("terminating connection due to administrator command"),
        psycopg.OperationalError("the connection is closed"),
    ],
)
def test_is_pool_unavailable_matches_outage_errors(exc: BaseException) -> None:
    assert is_pool_unavailable(exc) is True


def test_is_pool_unavailable_matches_wrapped_cause() -> None:
    cause = PoolTimeout("couldn't get a connection after 5.00 sec")
    try:
        try:
            raise cause
        except PoolTimeout as exc:
            raise RuntimeError("failed to list sessions") from exc
    except RuntimeError as wrapped:
        assert is_pool_unavailable(wrapped) is True


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("database unavailable"),
        psycopg.OperationalError("password authentication failed for user"),
        ValueError("nope"),
    ],
)
def test_is_pool_unavailable_rejects_other_errors(exc: BaseException) -> None:
    assert is_pool_unavailable(exc) is False


def test_throttled_logger_emits_once_per_interval() -> None:
    throttled = ThrottledLogger(interval_seconds=60.0)
    log = logging.getLogger("test.throttled")

    emitted: list[int] = []
    for _ in range(3):
        emitted.append(throttled(log, logging.WARNING, "hub temporarily unavailable"))

    assert emitted == [True, False, False]


def test_throttled_logger_keys_by_rendered_message() -> None:
    throttled = ThrottledLogger(interval_seconds=60.0)
    log = logging.getLogger("test.throttled")

    assert throttled(log, logging.WARNING, "hub down for %s", "a") is True
    assert throttled(log, logging.WARNING, "hub down for %s", "a") is False
    assert throttled(log, logging.WARNING, "hub down for %s", "b") is True
