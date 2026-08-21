"""Reap native process groups recorded on terminal rows."""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def reap_recorded_process(
    process: Mapping[str, Any],
    *,
    grace_seconds: float,
    now: float | None = None,
) -> None:
    """SIGTERM then SIGKILL a recorded pgid unless the start_time was recycled."""
    pgid_raw = process.get("pgid")
    start_raw = process.get("start_time")
    if not isinstance(pgid_raw, int) or pgid_raw <= 0:
        return
    start_time = float(start_raw) if isinstance(start_raw, (int, float)) else None
    if start_time is not None and _pid_recycled(pgid_raw, start_time):
        logger.info("Skipping recycled process group %s", pgid_raw)
        return
    _kill_group(pgid_raw, signal.SIGTERM)
    deadline = (now or time.time()) + max(0.0, grace_seconds)
    while time.time() < deadline:
        if not _group_alive(pgid_raw):
            return
        time.sleep(min(0.05, max(0.0, deadline - time.time())))
    _kill_group(pgid_raw, signal.SIGKILL)


def _pid_recycled(pgid: int, start_time: float) -> bool:
    try:
        import psutil

        proc = psutil.Process(pgid)
        created = float(proc.create_time())
        return abs(created - start_time) > 1.0
    except Exception:
        return False


def _kill_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        try:
            os.kill(pgid, sig)
        except ProcessLookupError:
            return
    except PermissionError:
        logger.warning("Permission denied signalling process group %s", pgid)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        try:
            os.kill(pgid, 0)
            return True
        except ProcessLookupError:
            return False
    except PermissionError:
        return True
