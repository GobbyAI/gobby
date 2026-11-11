"""The shared Codex app-server worker each hand-started thread registered from."""

from __future__ import annotations

import pytest

from gobby.storage.codex_thread_hosts import (
    THREAD_HOST_MAX_AGE_HOURS,
    ThreadHost,
    prune_thread_hosts,
    register_thread_host,
    thread_host,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

MACHINE = "machine-25905"
THREAD = "01a1c86f-0000-7000-8000-000000025905"
KEY = {"machine_id": MACHINE, "external_id": THREAD}
WORKER = ThreadHost(pid=78147, create_time=1_794_000_000.5)
OTHER_WORKER = ThreadHost(pid=78148, create_time=1_794_000_100.0)


def test_the_first_worker_to_register_a_thread_wins(temp_db: HubDatabase) -> None:
    assert thread_host(temp_db, **KEY) is None

    assert register_thread_host(temp_db, **KEY, host=WORKER)
    assert not register_thread_host(temp_db, **KEY, host=OTHER_WORKER)
    # The registered worker's later hooks keep its registration.
    assert register_thread_host(temp_db, **KEY, host=WORKER)

    assert thread_host(temp_db, **KEY) == WORKER
    assert thread_host(temp_db, machine_id="machine-other", external_id=THREAD) is None


def test_a_dead_worker_is_replaced_only_by_compare_and_swap(temp_db: HubDatabase) -> None:
    register_thread_host(temp_db, **KEY, host=WORKER)
    restarted = ThreadHost(pid=WORKER.pid, create_time=WORKER.create_time + 30)

    assert not register_thread_host(temp_db, **KEY, host=OTHER_WORKER, displaced=restarted)
    assert register_thread_host(temp_db, **KEY, host=OTHER_WORKER, displaced=WORKER)
    assert not register_thread_host(temp_db, **KEY, host=restarted, displaced=WORKER)

    assert thread_host(temp_db, **KEY) == OTHER_WORKER


def test_pruning_drops_only_registrations_no_hook_refreshed(temp_db: HubDatabase) -> None:
    quiet = {**KEY, "external_id": "01a1c86f-0000-7000-8000-000000025906"}
    register_thread_host(temp_db, **KEY, host=WORKER)
    register_thread_host(temp_db, **quiet, host=WORKER)
    temp_db.execute(
        """
        UPDATE codex_thread_hosts
        SET seen_at = clock_timestamp() - make_interval(hours => %s)
        """,
        (THREAD_HOST_MAX_AGE_HOURS + 1,),
    )
    # A hook from the registered worker refreshes its thread.
    register_thread_host(temp_db, **KEY, host=WORKER)

    assert prune_thread_hosts(temp_db) == 1
    assert thread_host(temp_db, **KEY) == WORKER
    assert thread_host(temp_db, **quiet) is None
