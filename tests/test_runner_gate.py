"""Tests for the bounded clean-child predecessor gate."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from typing import Any
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

    with (
        patch("gobby.runner_gate.psycopg.connect", return_value=connection) as connect,
        patch("gobby.runner_gate.time.sleep"),
    ):
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
        application_name=connect.call_args.kwargs["application_name"],
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
