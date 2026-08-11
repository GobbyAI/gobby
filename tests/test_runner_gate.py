"""Tests for the bounded clean-child predecessor gate."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby import runner_gate
from gobby.deployment import deployment_advisory_key


class _Cursor:
    def __init__(self, *, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _GateConnection:
    def __init__(self) -> None:
        self.stat_activity_reads = 0
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> _GateConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> Any:
        return nullcontext()

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _Cursor:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "FROM pg_stat_activity" in normalized:
            self.stat_activity_reads += 1
            return _Cursor(rows=[(41,)] if self.stat_activity_reads == 1 else [])
        return _Cursor(rows=[(True,)])


def test_gate_uses_terminal_fence_and_verifies_predecessor_exit() -> None:
    connection = _GateConnection()
    token = "deployment-token"
    fence_key = deployment_advisory_key("agent-terminal-transition", token=token)
    gate_application_name = f"gobby-gate-{token}-gateid12"

    with (
        patch("gobby.runner_gate.psycopg.connect", return_value=connection) as connect,
        patch("gobby.runner_gate.time.sleep"),
        patch("gobby.runner_gate.uuid.uuid4") as uuid4,
    ):
        uuid4.return_value.hex = "gateid1234567890"
        result = runner_gate._run_gate_request(
            {
                "conninfo": "postgresql://gate-secret",
                "deployment_token": token,
                "fence_key": fence_key,
                "successor_application_name": f"gobby-hub-{token}-successor",
                "budget_seconds": 20,
            }
        )

    assert result == {
        "success": True,
        "fence_key": fence_key,
    }
    connect.assert_called_once_with(
        "postgresql://gate-secret",
        autocommit=True,
        connect_timeout=runner_gate.GATE_CONNECT_TIMEOUT_SECONDS,
        application_name=gate_application_name,
    )
    statements = [sql for sql, _params in connection.calls]
    assert statements[:3] == [
        f"SET LOCAL statement_timeout = '{runner_gate.GATE_STATEMENT_TIMEOUT_MS}ms'",
        f"SET LOCAL lock_timeout = '{runner_gate.GATE_LOCK_TIMEOUT_MS}ms'",
        "SELECT pg_advisory_xact_lock(%s)",
    ]
    assert statements.count("SELECT pg_terminate_backend(%s, %s)") == 1
    terminate_call = next(
        params for sql, params in connection.calls if sql == "SELECT pg_terminate_backend(%s, %s)"
    )
    assert terminate_call == (41, runner_gate.GATE_TERMINATE_TIMEOUT_MS)
    assert connection.stat_activity_reads == 2


class _StalledProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.communicate_started = asyncio.Event()
        self.killed = False
        self.waited = False
        self.payload: bytes | None = None

    async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
        self.payload = payload
        self.communicate_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or 0


@pytest.mark.asyncio
async def test_gate_watchdog_kills_and_reaps_stalled_child() -> None:
    process = _StalledProcess()
    create_process = AsyncMock(return_value=process)

    with patch("gobby.runner_gate.asyncio.create_subprocess_exec", create_process):
        with pytest.raises(runner_gate.RunnerGateError, match="watchdog expired"):
            await runner_gate.acquire_runner_gate(
                "postgresql://gate-secret",
                successor_application_name="gobby-hub-token-successor",
                deadline_seconds=0.01,
            )

    assert process.killed is True
    assert process.waited is True
    assert "postgresql://gate-secret" not in repr(create_process.call_args)
    payload = json.loads(process.payload or b"{}")
    assert payload["conninfo"] == "postgresql://gate-secret"
    assert payload["fence_key"] == runner_gate._terminal_fence_key(payload["deployment_token"])


@pytest.mark.asyncio
async def test_gate_cancellation_reaps_child_before_propagating() -> None:
    process = _StalledProcess()
    create_process = AsyncMock(return_value=process)

    with patch("gobby.runner_gate.asyncio.create_subprocess_exec", create_process):
        gate_task = asyncio.create_task(
            runner_gate.acquire_runner_gate(
                "postgresql://gate-secret",
                successor_application_name="gobby-hub-token-successor",
                deadline_seconds=10,
            )
        )
        await process.communicate_started.wait()
        gate_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gate_task

    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_gate_cancellation_wins_over_reap_failure() -> None:
    process = _StalledProcess()
    reap_started = asyncio.Event()
    release_reap = asyncio.Event()

    async def fail_reap(_process: object) -> None:
        reap_started.set()
        await release_reap.wait()
        raise RuntimeError("reap failed")

    with patch("gobby.runner_gate._kill_and_reap", side_effect=fail_reap):
        task = asyncio.create_task(
            runner_gate._settle_reap_under_cancellation(cast(asyncio.subprocess.Process, process))
        )
        await reap_started.wait()
        task.cancel()
        release_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestRunnerGateLiveFence:
    """Plan 1.4.21: live advisory-fence scenarios against the test cluster."""

    @pytest.mark.asyncio
    async def test_perpetual_shared_holder_fails_closed_with_diagnostic(
        self, postgres_database_url: str, tmp_path: Any
    ) -> None:
        import time

        import psycopg

        from gobby.deployment import deployment_token
        from gobby.runner_gate import RunnerGateError, acquire_runner_gate

        token = deployment_token(tmp_path)
        fence_key = runner_gate._terminal_fence_key(token)

        with psycopg.connect(postgres_database_url) as holder:
            holder.execute("SELECT pg_advisory_xact_lock_shared(%s)", (fence_key,))
            started = time.monotonic()
            with pytest.raises(RunnerGateError, match="fence deadline expired"):
                await acquire_runner_gate(
                    postgres_database_url,
                    successor_application_name=f"gobby-hub-{token}-successor",
                    data_root=tmp_path,
                    deadline_seconds=4.0,
                )
            assert time.monotonic() - started < 20.0

    @pytest.mark.asyncio
    async def test_gate_blocks_until_shared_holder_resolves_then_acquires(
        self, postgres_database_url: str, tmp_path: Any
    ) -> None:
        import psycopg

        from gobby.deployment import deployment_token
        from gobby.runner_gate import acquire_runner_gate

        token = deployment_token(tmp_path)
        fence_key = runner_gate._terminal_fence_key(token)

        holder = psycopg.connect(postgres_database_url)
        probe = psycopg.connect(postgres_database_url, autocommit=True)
        try:
            holder.execute("SELECT pg_advisory_xact_lock_shared(%s)", (fence_key,))
            gate = asyncio.ensure_future(
                acquire_runner_gate(
                    postgres_database_url,
                    successor_application_name=f"gobby-hub-{token}-successor",
                    data_root=tmp_path,
                    deadline_seconds=30.0,
                )
            )
            # Release only after the child is observed waiting on the fence.
            classid = (fence_key >> 32) & 0xFFFFFFFF
            objid = fence_key & 0xFFFFFFFF
            for _ in range(200):
                waiting = probe.execute(
                    """
                    SELECT count(*) FROM pg_locks
                    WHERE locktype = 'advisory' AND NOT granted
                      AND classid = %s AND objid = %s
                    """,
                    (classid, objid),
                ).fetchone()
                if waiting and waiting[0]:
                    break
                if gate.done():
                    pytest.fail("gate unexpectedly completed while the shared fence was held")
                await asyncio.wait({gate}, timeout=0.1)
            else:
                pytest.fail("gate child never blocked on the shared fence")
            assert not gate.done()
            holder.commit()
            await asyncio.wait_for(gate, timeout=25.0)
        finally:
            holder.close()
            probe.close()

    @pytest.mark.asyncio
    async def test_severance_terminates_predecessor_markers_and_spares_successor(
        self, postgres_database_url: str, tmp_path: Any
    ) -> None:
        import psycopg

        from gobby.deployment import deployment_token
        from gobby.runner_gate import acquire_runner_gate

        token = deployment_token(tmp_path)
        successor_name = f"gobby-hub-{token}-successor"

        predecessor = psycopg.connect(
            postgres_database_url,
            autocommit=True,
            application_name=f"gobby-hub-{token}-predecessor",
        )
        predecessor_listener = psycopg.connect(
            postgres_database_url,
            autocommit=True,
            application_name=f"gobby-hub-{token}-predecessor-listener",
        )
        successor_marker = psycopg.connect(
            postgres_database_url,
            autocommit=True,
            application_name=successor_name,
        )
        # The successor hub derives its config LISTEN connection name from its
        # own pool name; the gate must spare the whole successor family or it
        # severs the daemon it is fencing for (#20065).
        successor_listener = psycopg.connect(
            postgres_database_url,
            autocommit=True,
            application_name=f"{successor_name}-listener",
        )
        try:
            await acquire_runner_gate(
                postgres_database_url,
                successor_application_name=successor_name,
                data_root=tmp_path,
                deadline_seconds=30.0,
            )
            with pytest.raises(psycopg.OperationalError):
                predecessor.execute("SELECT 1")
            with pytest.raises(psycopg.OperationalError):
                predecessor_listener.execute("SELECT 1")
            assert successor_marker.execute("SELECT 1").fetchone() == (1,)
            assert successor_listener.execute("SELECT 1").fetchone() == (1,)
        finally:
            predecessor.close()
            predecessor_listener.close()
            successor_marker.close()
            successor_listener.close()
