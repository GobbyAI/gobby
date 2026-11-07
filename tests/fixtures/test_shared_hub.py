"""A maintenance wait on a shared test-hub lock is bounded and names its holder."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import tests.fixtures.shared_hub as shared_hub
from tests.fixtures.postgres import isolated_test_database
from tests.fixtures.shared_hub import SharedHubLockWait

pytestmark = pytest.mark.integration

# A second, independent session holds SHARE on pg_database inside an open transaction,
# the shape a stale test run leaves behind. CREATE DATABASE then waits on it.
_HOLDER = """
import sys
import time

import psycopg

conn = psycopg.connect(sys.argv[1], autocommit=False)
conn.execute("LOCK TABLE pg_catalog.pg_database IN SHARE MODE")
print("holding", conn.info.backend_pid, flush=True)
time.sleep(120)
"""


def test_maintenance_wait_on_a_hub_lock_names_the_holder(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared_hub, "HUB_LOCK_WAIT_SECONDS", 2)
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", _HOLDER, postgres_database_url],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        text=True,
    )
    outcome: dict[str, Any] = {}

    def create_isolated_database() -> None:
        try:
            with isolated_test_database(postgres_database_url):
                outcome["created"] = True
        except SharedHubLockWait as exc:
            outcome["error"] = exc

    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().startswith("holding")
        started = time.monotonic()
        worker = threading.Thread(target=create_isolated_database, daemon=True)
        worker.start()
        worker.join(timeout=20)
        # Before the fix the create waits forever behind the holder.
        assert not worker.is_alive(), "maintenance CREATE DATABASE waited with no bound"
        elapsed = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait()

    error = outcome.get("error")
    assert isinstance(error, SharedHubLockWait), outcome
    message = str(error)
    assert elapsed < 60
    assert f"pid {holder.pid}" in message
    assert str(tmp_path.resolve()) in message
    assert "age" in message


def test_free_hub_creates_and_drops_the_isolated_database(postgres_database_url: str) -> None:
    with isolated_test_database(postgres_database_url) as database_url:
        assert database_url != postgres_database_url
