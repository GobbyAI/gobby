"""Bounded predecessor fence for daemon startup.

The parent launches this module in a clean interpreter and passes the DSN on
stdin.  The child uses one direct PostgreSQL connection, acquires the terminal
writer fence exclusively, severs predecessor hub backends, verifies their
exit, and then commits the read-only gate transaction.  The parent watchdog
always kills and reaps a stalled child before startup can unwind.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg

from gobby.deployment import deployment_advisory_key, deployment_token

logger = logging.getLogger(__name__)

GATE_DEADLINE_SECONDS = 60.0
GATE_CONNECT_TIMEOUT_SECONDS = 5
GATE_LOCK_TIMEOUT_MS = 5_000
GATE_STATEMENT_TIMEOUT_MS = 6_000
GATE_DIAGNOSTIC_TIMEOUT_MS = 2_000
GATE_TERMINATE_TIMEOUT_MS = 1_000
GATE_RETRY_BACKOFF_SECONDS = 0.1
GATE_REAP_TIMEOUT_SECONDS = 2.0


class RunnerGateError(RuntimeError):
    """Raised when predecessor fencing cannot complete within its budget."""


def _terminal_fence_key(token: str) -> int:
    return deployment_advisory_key("agent-terminal-transition", token=token)


def _predecessor_pids(
    conn: psycopg.Connection[Any],
    *,
    hub_prefix: str,
    successor_application_name: str,
) -> list[int]:
    rows = conn.execute(
        """
        SELECT pid
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND application_name LIKE %s
          AND application_name <> %s
        ORDER BY pid
        """,
        (f"{hub_prefix}%", successor_application_name),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _sever_predecessor_backends(
    conn: psycopg.Connection[Any],
    *,
    hub_prefix: str,
    successor_application_name: str,
    deadline: float,
) -> None:
    """Terminate and verify every other lifecycle backend under the fence."""
    while True:
        # pg_stat_activity is a per-transaction snapshot; without clearing it,
        # a re-query inside the fence transaction can never observe a
        # terminated backend's exit and the verified-dead loop spins to the
        # gate deadline.
        conn.execute("SELECT pg_stat_clear_snapshot()").fetchone()
        predecessor_pids = _predecessor_pids(
            conn,
            hub_prefix=hub_prefix,
            successor_application_name=successor_application_name,
        )
        if not predecessor_pids:
            return
        if time.monotonic() >= deadline:
            raise RunnerGateError(
                f"Predecessor backends remained alive after termination: {predecessor_pids}"
            )
        for pid in predecessor_pids:
            conn.execute(
                "SELECT pg_terminate_backend(%s, %s)",
                (pid, GATE_TERMINATE_TIMEOUT_MS),
            ).fetchone()
        time.sleep(0.05)


def _diagnose_gate_wait(
    conn: psycopg.Connection[Any],
    *,
    fence_key: int,
    hub_prefix: str,
    successor_application_name: str,
) -> str:
    """Return bounded fallback diagnostics without weakening a failed gate."""
    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL statement_timeout = '{GATE_DIAGNOSTIC_TIMEOUT_MS}ms'")
            pids = _predecessor_pids(
                conn,
                hub_prefix=hub_prefix,
                successor_application_name=successor_application_name,
            )
        return f"fence_key={fence_key}, predecessor_pids={pids}"
    except BaseException as exc:
        return f"fence_key={fence_key}, diagnostic_error={type(exc).__name__}: {exc}"


def _run_gate_request(request: dict[str, Any]) -> dict[str, Any]:
    """Execute the child-side gate and return a JSON-safe result."""
    conninfo = request.get("conninfo")
    token = request.get("deployment_token")
    fence_key = request.get("fence_key")
    successor_application_name = request.get("successor_application_name")
    budget_seconds = request.get("budget_seconds", GATE_DEADLINE_SECONDS)
    if not isinstance(conninfo, str) or not conninfo:
        raise ValueError("Gate request requires conninfo")
    if not isinstance(token, str) or not token:
        raise ValueError("Gate request requires deployment_token")
    if isinstance(fence_key, bool) or not isinstance(fence_key, int):
        raise ValueError("Gate request requires an integer fence_key")
    if fence_key != _terminal_fence_key(token):
        raise ValueError("Gate request fence_key does not match deployment_token")
    if not isinstance(successor_application_name, str):
        raise ValueError("Gate request requires successor_application_name")
    if not isinstance(budget_seconds, int | float) or budget_seconds <= 0:
        raise ValueError("Gate request requires a positive budget_seconds")

    deadline = time.monotonic() + float(budget_seconds)
    hub_prefix = f"gobby-hub-{token}-"
    gate_application_name = f"gobby-gate-{token}-{uuid.uuid4().hex[:8]}"
    minimum_attempt_budget = GATE_CONNECT_TIMEOUT_SECONDS + (GATE_STATEMENT_TIMEOUT_MS / 1_000)

    with psycopg.connect(
        conninfo,
        autocommit=True,
        connect_timeout=GATE_CONNECT_TIMEOUT_SECONDS,
        application_name=gate_application_name,
    ) as conn:
        last_lock_error: BaseException | None = None
        while deadline - time.monotonic() >= minimum_attempt_budget:
            lock_timed_out = False
            try:
                with conn.transaction():
                    conn.execute(f"SET LOCAL statement_timeout = '{GATE_STATEMENT_TIMEOUT_MS}ms'")
                    conn.execute(f"SET LOCAL lock_timeout = '{GATE_LOCK_TIMEOUT_MS}ms'")
                    try:
                        conn.execute("SELECT pg_advisory_xact_lock(%s)", (fence_key,)).fetchone()
                    except psycopg.errors.LockNotAvailable as exc:
                        lock_timed_out = True
                        last_lock_error = exc
                        raise
                    _sever_predecessor_backends(
                        conn,
                        hub_prefix=hub_prefix,
                        successor_application_name=successor_application_name,
                        deadline=deadline,
                    )
                return {"success": True, "fence_key": fence_key}
            except psycopg.errors.LockNotAvailable:
                if not lock_timed_out:
                    raise
                time.sleep(GATE_RETRY_BACKOFF_SECONDS)

        detail = _diagnose_gate_wait(
            conn,
            fence_key=fence_key,
            hub_prefix=hub_prefix,
            successor_application_name=successor_application_name,
        )
        error_suffix = (
            f"; last_lock_error={type(last_lock_error).__name__}: {last_lock_error}"
            if last_lock_error is not None
            else ""
        )
        raise RunnerGateError(f"Predecessor fence deadline expired ({detail}){error_suffix}")


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """Kill a gate child if needed and always reap its process handle."""
    if process.returncode is None:
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=GATE_REAP_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RunnerGateError("Gate child did not exit after kill") from exc


async def _settle_reap_under_cancellation(process: asyncio.subprocess.Process) -> None:
    owned = asyncio.create_task(_kill_and_reap(process), name="runner-gate-reap")
    cancellation: asyncio.CancelledError | None = None
    while not owned.done():
        try:
            await asyncio.shield(owned)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    owned.result()
    if cancellation is not None:
        raise cancellation


async def acquire_runner_gate(
    conninfo: str,
    *,
    successor_application_name: str,
    data_root: Path | None = None,
    deadline_seconds: float = GATE_DEADLINE_SECONDS,
) -> None:
    """Run the bounded clean-child predecessor gate before daemon activation."""
    if deadline_seconds <= 0:
        raise ValueError("Runner gate deadline must be positive")
    token = deployment_token(data_root)
    fence_key = _terminal_fence_key(token)
    request = json.dumps(
        {
            "conninfo": conninfo,
            "deployment_token": token,
            "fence_key": fence_key,
            "successor_application_name": successor_application_name,
            "budget_seconds": deadline_seconds,
        }
    ).encode()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "gobby.runner_gate",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(request),
            timeout=deadline_seconds,
        )
    except TimeoutError as exc:
        await _settle_reap_under_cancellation(process)
        raise RunnerGateError(
            f"Predecessor gate watchdog expired after {deadline_seconds:g}s"
        ) from exc
    except BaseException:
        await _settle_reap_under_cancellation(process)
        raise

    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        raise RunnerGateError(f"Predecessor gate child failed: {detail or process.returncode}")
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RunnerGateError("Predecessor gate child returned invalid output") from exc
    if not isinstance(response, dict) or response.get("success") is not True:
        error = response.get("error") if isinstance(response, dict) else None
        raise RunnerGateError(str(error or "Predecessor gate failed"))


def _main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read())
        if not isinstance(request, dict):
            raise ValueError("Gate request must be a JSON object")
        response = _run_gate_request(request)
    except BaseException as exc:
        response = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    os._exit(_main())


__all__ = ["RunnerGateError", "acquire_runner_gate"]
