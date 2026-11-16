"""Canonical terminal-session identity and ownership ordering."""

from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import psutil

from gobby.storage.sessions._constants import LIVE_SESSION_STATUS_ORDER
from gobby.utils import spawn

TERMINAL_OWNER_STATUSES = LIVE_SESSION_STATUS_ORDER


class _ProcessLike(Protocol):
    pid: int

    def create_time(self) -> float: ...

    def parents(self) -> list[Any]: ...


class OwnershipState(str, Enum):
    OWNED = "owned"
    OWNERLESS = "ownerless"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ForegroundOwnershipInspection:
    state: OwnershipState
    process: _ProcessLike | None = None


def _normalized_parent_pid(session: object) -> int | None:
    terminal_context = getattr(session, "terminal_context", None)
    if not isinstance(terminal_context, Mapping):
        return None
    value = terminal_context.get("parent_pid")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _recorded_create_time(session: object) -> float | None:
    terminal_context = getattr(session, "terminal_context", None)
    if not isinstance(terminal_context, Mapping):
        return None
    value = terminal_context.get("parent_create_time")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        create_time = float(value)
        return create_time if math.isfinite(create_time) and create_time > 0 else None
    except (TypeError, ValueError):
        return None


def foreground_process_group(
    pid: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = spawn.run,
) -> int:
    """Return the terminal foreground process group reported for *pid*."""
    result = runner(
        ["ps", "-o", "tpgid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    output = result.stdout.strip()
    error = result.stderr.strip()
    if result.returncode != 0:
        if not output and (not error or "no such process" in error.lower()):
            raise ProcessLookupError(pid)
        raise OSError(error or f"ps exited with status {result.returncode}")
    fields = output.split()
    if len(fields) != 1:
        raise OSError(f"unexpected ps tpgid output: {output!r}")
    try:
        return int(fields[0])
    except ValueError as exc:
        raise OSError(f"unexpected ps tpgid output: {output!r}") from exc


def inspect_foreground_ownership(
    session: object,
    *,
    process_factory: Callable[[int], _ProcessLike] = psutil.Process,
    process_group_factory: Callable[[int], int] = os.getpgid,
    foreground_group_factory: Callable[[int], int] = foreground_process_group,
) -> ForegroundOwnershipInspection:
    """Validate the recorded CLI process and its foreground terminal ownership."""
    pid = _normalized_parent_pid(session)
    expected_create_time = _recorded_create_time(session)
    if pid is None or expected_create_time is None:
        return ForegroundOwnershipInspection(OwnershipState.OWNERLESS)
    try:
        process = process_factory(pid)
        if abs(process.create_time() - expected_create_time) >= 1.0:
            return ForegroundOwnershipInspection(OwnershipState.OWNERLESS)
        process_group = process_group_factory(pid)
    except (TypeError, ValueError, psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError):
        return ForegroundOwnershipInspection(OwnershipState.OWNERLESS)
    except (psutil.AccessDenied, PermissionError, OSError):
        return ForegroundOwnershipInspection(OwnershipState.INDETERMINATE)

    try:
        foreground_group = foreground_group_factory(pid)
    except ProcessLookupError:
        return ForegroundOwnershipInspection(OwnershipState.OWNERLESS)
    except (TypeError, ValueError, subprocess.SubprocessError, PermissionError, OSError):
        return ForegroundOwnershipInspection(OwnershipState.INDETERMINATE)
    if foreground_group <= 0 or process_group != foreground_group:
        return ForegroundOwnershipInspection(OwnershipState.OWNERLESS)
    return ForegroundOwnershipInspection(OwnershipState.OWNED, process)


def recorded_process_is_alive(session: object) -> bool:
    """Whether the session's recorded CLI process still runs on this node.

    The recorded start time guards against PID reuse; a process that cannot be
    inspected counts as alive.
    """
    pid = _normalized_parent_pid(session)
    expected_create_time = _recorded_create_time(session)
    if pid is None or expected_create_time is None:
        return False
    try:
        process: _ProcessLike = psutil.Process(pid)
        return abs(process.create_time() - expected_create_time) < 1.0
    except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError):
        return False
    except (psutil.AccessDenied, PermissionError, OSError):
        return True


def recorded_process_exited(session: object) -> bool:
    """Whether the session recorded a CLI process that no longer runs.

    Unlike :func:`recorded_process_is_alive`, a session that recorded no
    process identity reports False: nothing proves it exited.
    """
    if _normalized_parent_pid(session) is None or _recorded_create_time(session) is None:
        return False
    return not recorded_process_is_alive(session)


def recorded_seat_left(
    session: object,
    *,
    inspect: Callable[[object], ForegroundOwnershipInspection] = inspect_foreground_ownership,
) -> bool:
    """Whether the CLI process the session recorded no longer owns its terminal.

    Keys typed into such a pane reach whatever runs there now. A session that
    recorded no process identity names no seat to lose, and an uninspectable
    seat counts as present, as it does for the liveness monitor.
    """
    if _normalized_parent_pid(session) is None or _recorded_create_time(session) is None:
        return False
    return inspect(session).state is OwnershipState.OWNERLESS
