"""A maintenance wait on a shared test-hub lock is bounded and names its holder."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psycopg
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


# Each fake tool runs for this long on the virtual clock. A report that makes one call per
# host process overruns the diagnostics budget long before it finishes.
_SLOW_TOOL_SECONDS = 8
_OWNER_COUNT = 12


class _VirtualClock:
    """A monotonic clock that moves only when a fake tool runs."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _fake_tool_output(argv: list[str], hub_port: int, owners: list[int]) -> str:
    if argv[0] == "lsof" and any(arg.startswith("-iTCP:") for arg in argv):
        return "".join(f"p{pid}\nn127.0.0.1:{pid + 1000}->127.0.0.1:{hub_port}\n" for pid in owners)
    pids = argv[argv.index("-p") + 1].split(",")
    if argv[0] == "lsof":
        return "".join(f"p{pid}\nn/work/{pid}\n" for pid in pids)
    return "".join(f"{pid} 00:05\n" for pid in pids)


def test_holder_report_stays_inside_its_budget_when_tools_are_slow(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _VirtualClock()
    calls: list[list[str]] = []
    owners = [40000 + index for index in range(_OWNER_COUNT)]

    def fake_run(argv: list[str], *, timeout: float, **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if _SLOW_TOOL_SECONDS > timeout:
            clock.now += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        clock.now += _SLOW_TOOL_SECONDS
        stdout = _fake_tool_output(argv, hub_port, owners)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        hub_port = int(conn.info.port or 0)
        monkeypatch.setattr(shared_hub, "_monotonic", clock)
        monkeypatch.setattr(subprocess, "run", fake_run)
        message = shared_hub._describe_holders(conn)

    assert clock.now <= shared_hub.HUB_DIAGNOSTICS_SECONDS
    assert len(calls) <= 3
    assert "diagnostics budget" in message
