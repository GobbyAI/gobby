"""Rate-limited pg_stat_activity census for hub pool acquisition failures."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

ACTIVITY_CENSUS_INTERVAL_SECONDS = 60.0
ACTIVITY_CENSUS_THREAD_NAME = "gobby-hub-activity-census"
_ACTIVITY_CENSUS_SQL = (
    "SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1, 2"
)
# connect_timeout bounds the connect, statement_timeout bounds the query on the
# server, and keepalives end a reply lost to a dead peer in about 9 seconds. A
# live server that holds the socket silently is unbounded; that stall pins only
# the one census thread, and in_flight keeps later failures from starting more.
_ACTIVITY_CENSUS_CONNECT_KWARGS: dict[str, Any] = {
    "application_name": ACTIVITY_CENSUS_THREAD_NAME,
    "connect_timeout": 2,
    "options": "-c statement_timeout=2000",
    "keepalives": 1,
    "keepalives_idle": 5,
    "keepalives_interval": 2,
    "keepalives_count": 2,
}
_TOO_MANY_CONNECTIONS_SQLSTATE = "53300"


@dataclass
class _ActivityCensusGate:
    """At most one census in flight and at most one started per interval."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    last_started_at: float | None = None
    in_flight: bool = False

    def try_begin(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            if self.in_flight or (
                self.last_started_at is not None
                and now - self.last_started_at < ACTIVITY_CENSUS_INTERVAL_SECONDS
            ):
                return False
            self.last_started_at = now
            self.in_flight = True
            return True
        finally:
            self.lock.release()

    def finish(self) -> None:
        with self.lock:
            self.in_flight = False


_ACTIVITY_CENSUS = _ActivityCensusGate()


def maybe_start_activity_census(
    pool: ConnectionPool[Any],
    pool_stats: Callable[[], dict[str, Any]],
) -> None:
    """Start a background pg_stat_activity census without delaying the caller.

    Never raises: the caller re-raises its original acquisition error next.
    """
    gate = _ACTIVITY_CENSUS
    if not gate.try_begin():
        return
    try:
        threading.Thread(
            target=_run_activity_census,
            args=(gate, pool.conninfo, pool_stats),
            name=ACTIVITY_CENSUS_THREAD_NAME,
            daemon=True,
        ).start()
    except Exception as exc:
        gate.finish()
        logger.warning(
            "PostgreSQL hub pool activity census not started: error=%s", type(exc).__name__
        )


def _run_activity_census(
    gate: _ActivityCensusGate,
    conninfo: str,
    pool_stats: Callable[[], dict[str, Any]],
) -> None:
    try:
        conn = psycopg.connect(conninfo, **_ACTIVITY_CENSUS_CONNECT_KWARGS)
        try:
            rows = conn.execute(_ACTIVITY_CENSUS_SQL).fetchall()
        finally:
            conn.close()
        logger.info(
            "PostgreSQL hub pool activity census: pool_stats=%s activity=%s",
            pool_stats(),
            rows,
        )
    except Exception as exc:
        # Exception text can carry the DSN (#22962), so only the class and SQLSTATE are logged.
        sqlstate = getattr(exc, "sqlstate", None)
        reason = (
            "server connection capacity exhausted"
            if sqlstate == _TOO_MANY_CONNECTIONS_SQLSTATE
            else "census could not run"
        )
        logger.warning(
            "PostgreSQL hub pool activity census failed: %s (error=%s sqlstate=%s)",
            reason,
            type(exc).__name__,
            sqlstate,
        )
    finally:
        gate.finish()
