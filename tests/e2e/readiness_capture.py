"""Bounded diagnostics for a fixture-owned process before timeout teardown."""

from __future__ import annotations

import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class ReadinessCapture:
    thread_stack_tail: str
    task_stack_tail: str
    startup_timing_tail: str


def safe_backend_state(response: httpx.Response) -> str | None:
    """Keep only the front door's two backend-state enum values."""
    if response.status_code != 503:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    backend = body.get("backend") if isinstance(body, dict) else None
    state = backend.get("state") if isinstance(backend, dict) else None
    return state if isinstance(state, str) and state in ("down", "starting") else None


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 16000))
            return stream.read().decode(errors="replace") or "<empty>"
    except OSError:
        return "<unavailable>"


def capture_readiness_timeout(
    log_dir: Path | None, process: subprocess.Popen[bytes] | None
) -> ReadinessCapture:
    if log_dir is None:
        return ReadinessCapture("<unavailable>", "<unavailable>", "<unavailable>")
    threads = log_dir / "startup-threads.log"
    tasks = log_dir / "startup-tasks.log"
    timings = log_dir / "startup-timings.jsonl"
    # Only the diagnostic bootstrap opts in. Never signal a legacy/custom
    # process whose handlers might use these signals for another purpose.
    try:
        owner_pid = int((log_dir / "readiness-diagnostics.pid").read_text())
    except (OSError, ValueError):
        owner_pid = None
    if (
        owner_pid is not None
        and process is not None
        and owner_pid == process.pid
        and process.poll() is None
    ):
        try:
            process.send_signal(signal.SIGUSR2)
            process.send_signal(signal.SIGUSR1)
        except OSError:
            pass  # Process exited between poll and signal; retain existing evidence.
        else:
            deadline = time.monotonic() + 2.0
            ready = log_dir / "readiness-tasks.ready"
            while time.monotonic() < deadline and (
                _tail(threads) in ("<unavailable>", "<empty>")
                or _tail(ready).strip() != str(process.pid)
            ):
                time.sleep(0.025)
    return ReadinessCapture(_tail(threads), _tail(tasks), _tail(timings))
