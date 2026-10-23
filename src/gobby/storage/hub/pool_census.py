"""Rate-limited pg_stat_activity census for hub pool acquisition failures."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from psycopg_pool import ConnectionPool

ACTIVITY_CENSUS_INTERVAL_SECONDS = 60.0
ACTIVITY_CENSUS_THREAD_NAME = "gobby-hub-activity-census"


@dataclass
class _ActivityCensusGate:
    """At most one census in flight and at most one started per interval."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    last_started_at: float | None = None
    in_flight: bool = False


_ACTIVITY_CENSUS = _ActivityCensusGate()


def maybe_start_activity_census(
    pool: ConnectionPool[Any],
    pool_stats: Callable[[], dict[str, Any]],
) -> None:
    """Behavior-free stub; the census is not implemented yet."""
    return None
