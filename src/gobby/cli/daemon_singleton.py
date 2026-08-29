"""Start, stop, status, and health routing for the role-bearing singleton."""

from __future__ import annotations

from pathlib import Path

from gobby.runner_pid_file import (
    PidFileClaim,
    ProbeState,
    SingletonProbe,
    SingletonReservationError,
    cancel_service_reservation,
    claim_pid_file,
    prepare_commanded_service_start,
    probe_daemon_lock,
    reserve_service_start,
)


def format_singleton_status(probe: SingletonProbe) -> str:
    label = probe.state.value.replace("_", " ")
    if probe.state is ProbeState.DAEMON and probe.pid is not None:
        return f"Gobby daemon: running (PID: {probe.pid})"
    if probe.state is ProbeState.MAINTENANCE:
        pid = f" (PID: {probe.pid})" if probe.pid is not None else ""
        return f"Gobby singleton: maintenance{pid}"
    if probe.state is ProbeState.ABSENT:
        return "Gobby daemon: not running"
    # A start window is exactly when an operator runs `status`, and the question is
    # binary: is it coming back, or is it stuck? The raw ProbeState name answers
    # neither, so both reservation states say which one it is (#21240).
    if probe.state is ProbeState.LIVE_RESERVATION:
        pid = f" (PID: {probe.pid})" if probe.pid is not None else ""
        return f"Gobby daemon: starting{pid}; a start reservation is live, retry shortly"
    if probe.state is ProbeState.STALE_RESERVATION:
        return "Gobby daemon: not running; an earlier start did not finish, run `gobby start`"
    return f"Gobby singleton: {label}"


def probe_start_blocker(probe: SingletonProbe) -> str | None:
    if probe.state is ProbeState.ABSENT:
        return None
    if probe.state is ProbeState.STALE_RESERVATION:
        return None
    if probe.state is ProbeState.DAEMON:
        pid = probe.pid or "unknown"
        return f"Daemon already running (PID: {pid})"
    if probe.state is ProbeState.MAINTENANCE:
        return "A maintenance campaign holds the singleton"
    if probe.state is ProbeState.LIVE_RESERVATION:
        return "A service start reservation is already live"
    if probe.state is ProbeState.TRANSITIONING:
        return "Singleton is transitioning"
    return f"Singleton is busy ({probe.state.value})"


def admit_direct_start(pid_file: Path) -> tuple[PidFileClaim | None, str | None]:
    blocker = probe_start_blocker(probe_daemon_lock(pid_file))
    if blocker:
        return None, blocker
    claim = claim_pid_file(pid_file, role="daemon")
    if claim is None:
        blocker = probe_start_blocker(probe_daemon_lock(pid_file))
        return None, blocker or "Could not claim the daemon singleton"
    return claim, None


def admit_service_start(pid_file: Path, *, backend: str) -> str | None:
    try:
        reserve_service_start(pid_file, backend=backend)
    except SingletonReservationError as exc:
        return str(exc)
    return None


def service_backend_name(platform: str | None) -> str:
    if platform in {"macos", "darwin", "launchd"}:
        return "launchd"
    if platform in {"linux", "systemd"}:
        return "systemd"
    if platform in {"windows", "win32"}:
        return "windows"
    return platform or "launchd"


def stop_singleton_gate(pid_file: Path) -> tuple[str, str | None]:
    """Inspect the singleton before stop effects.

    Returns ``("refuse", error)``, ``("cancelled", None)``, or ``("continue", None)``.
    """
    probe = probe_daemon_lock(pid_file)
    if probe.state is ProbeState.MAINTENANCE:
        return "refuse", "A maintenance campaign holds the singleton"
    if probe.state is ProbeState.TRANSITIONING:
        return "refuse", "Singleton is transitioning"
    if probe.state is ProbeState.LIVE_RESERVATION:
        cancel_service_reservation(pid_file)
        return "cancelled", None
    return "continue", None


def commanded_service_admission() -> str | None:
    try:
        prepare_commanded_service_start()
    except SingletonReservationError as exc:
        return str(exc)
    return None
