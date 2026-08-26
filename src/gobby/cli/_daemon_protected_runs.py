"""Restart-protected cron run gate for ``gobby stop`` and ``gobby restart``.

A restart-protected cron job (the nightly memory dream) holds a lease while its
run is active: the daemon reports such runs, and the CLI refuses to stop the
daemon underneath one unless told to wait or to force the interruption.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from gobby.utils.local_token import daemon_auth_headers

from .utils_process import format_uptime

PROTECTED_RUN_POLL_INTERVAL_SECONDS = 15.0
_FETCH_TIMEOUT_SECONDS = 3.0

StepFn = Callable[..., None]
FetchFn = Callable[[int], list[dict[str, Any]]]


def fetch_protected_runs(http_port: int) -> list[dict[str, Any]]:
    """Ask the daemon for its active restart-protected cron runs.

    The admin API requires the local CLI token; without it the daemon answers
    401 and the gate would silently never protect anything. A daemon that
    cannot answer holds no lease this process could honor, so transport
    failures and non-200 responses report no runs.
    """
    try:
        response = httpx.get(
            f"http://localhost:{http_port}/api/admin/cron/protected-runs",
            headers=daemon_auth_headers(),
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return []
    if response.status_code != 200:
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    runs = payload.get("runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict)]


def describe_protected_run(run: dict[str, Any]) -> str:
    """Render one protected run as ``name (running 1h 2m, at most 3h 28m left)``."""
    name = str(run.get("job_name") or run.get("run_id") or "cron run")
    elapsed = format_uptime(float(run.get("elapsed_seconds") or 0.0))
    remaining = format_uptime(float(run.get("remaining_seconds") or 0.0))
    return f"{name} (running {elapsed}, at most {remaining} left)"


def clear_protected_runs(
    http_port: int,
    *,
    force: bool,
    wait: bool,
    step: StepFn,
    fetch: FetchFn = fetch_protected_runs,
) -> bool:
    """Return whether shutdown may proceed under the protected-run policy.

    ``force`` proceeds after naming what it interrupts; ``wait`` polls until
    every protected run reaches a terminal state, bounded by the runs' own
    remaining timeouts; otherwise an active protected run refuses the stop.
    """
    runs = fetch(http_port)
    if not runs:
        return True
    if force:
        for run in runs:
            step(
                f"Interrupting protected cron run {describe_protected_run(run)}; "
                "it resumes after the next start"
            )
        return True
    if not wait:
        for run in runs:
            step(f"Protected cron run active: {describe_protected_run(run)}", error=True)
        step(
            "Refusing to stop the daemon; re-run with --wait to defer until it finishes "
            "or --force to interrupt it now",
            error=True,
        )
        return False

    longest = max(float(run.get("remaining_seconds") or 0.0) for run in runs)
    deadline = time.monotonic() + longest + PROTECTED_RUN_POLL_INTERVAL_SECONDS
    step(
        "Waiting for protected cron run(s) to finish: "
        + ", ".join(describe_protected_run(run) for run in runs)
    )
    while runs:
        if time.monotonic() > deadline:
            step(
                "Protected cron run(s) still active past their own timeout; refusing to stop",
                error=True,
            )
            return False
        time.sleep(PROTECTED_RUN_POLL_INTERVAL_SECONDS)
        runs = fetch(http_port)
    step("Protected cron run(s) finished")
    return True


__all__ = [
    "PROTECTED_RUN_POLL_INTERVAL_SECONDS",
    "clear_protected_runs",
    "describe_protected_run",
    "fetch_protected_runs",
]
