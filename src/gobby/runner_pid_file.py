"""Exclusive role-bearing PID-file ownership for the daemon process."""

from __future__ import annotations

import errno
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from gobby.runner_pid_record import (
    RECORD_VERSION,
    RESERVATION_AGE_BOUND_SECONDS,
    TRANSITIONING_STATE,
    SingletonFsyncError,
    SingletonRecordError,
    current_boot_id,
    current_time,
    next_generation,
    read_record_from_fd,
    read_record_from_path,
    reservation_is_live,
    truncate_record,
    write_record,
    write_transitioning_record,
)
from gobby.runner_service_reservation import (
    NonceError,
    consume_service_nonce_file,
    create_service_nonce_file,
    service_nonce_path,
    unlink_matching_nonce,
)

SERVICE_LAUNCH_ENV = "GOBBY_SERVICE_LAUNCH"
SERVICE_NONCE_ENV = "GOBBY_SERVICE_NONCE"
INHERITED_LOCK_FD_ENV = "GOBBY_SINGLETON_LOCK_FD"


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, fd: int, operation: int) -> None: ...


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int) -> None: ...


if os.name == "posix":  # pragma: no cover - platform dependent
    import fcntl as _fcntl_import

    _fcntl: _FcntlModule | None = _fcntl_import
else:  # pragma: no cover - Windows only
    _fcntl = None

if os.name == "nt":  # pragma: no cover - Windows only
    import msvcrt as _msvcrt_import

    _msvcrt: _MsvcrtModule | None = cast(_MsvcrtModule, _msvcrt_import)
else:  # pragma: no cover - Unix only
    _msvcrt = None


class SingletonError(Exception):
    """Typed singleton refusal with zero protected effects."""


class SingletonOpenError(SingletonError):
    """Opening the singleton lock file failed."""


class SingletonLockError(SingletonError):
    """Acquiring the singleton flock failed for a reason other than contention."""


class SingletonReservationError(SingletonError):
    """Service reservation mint, convert, or consume was refused."""


class ProbeState(StrEnum):
    ABSENT = "absent"
    DAEMON = "daemon"
    MAINTENANCE = "maintenance"
    LIVE_RESERVATION = "live_reservation"
    STALE_RESERVATION = "stale_reservation"
    TRANSITIONING = "transitioning"


@dataclass(frozen=True)
class ServiceReservationView:
    backend: str
    nonce: str
    nonce_path: str
    issued_at: float
    boot_id: str


@dataclass(frozen=True)
class SingletonProbe:
    state: ProbeState
    pid: int | None = None
    role: str | None = None
    reservation: ServiceReservationView | None = None
    generation: int | None = None
    boot_id: str | None = None

    def is_live_daemon(self) -> bool:
        return self.state is ProbeState.DAEMON


class PidFileClaim:
    """A held advisory lock protecting a daemon PID file."""

    def __init__(
        self,
        lock_path: Path,
        lock_fd: int,
        *,
        role: str = "daemon",
        generation: int = 1,
    ) -> None:
        self.lock_path = lock_path
        self._lock_fd = lock_fd
        self.role = role
        self.generation = generation
        self._released = False

    def fileno(self) -> int:
        return self._lock_fd

    def inherit_environment(self) -> dict[str, str]:
        return {INHERITED_LOCK_FD_ENV: str(self._lock_fd)}

    def detach(self) -> None:
        """Close the local descriptor without unlocking the inherited flock."""
        if self._released:
            return
        os.close(self._lock_fd)
        self._released = True

    def release(self) -> None:
        """Release the held lock without modifying the PID file."""
        if self._released:
            return
        try:
            _unlock_file(self._lock_fd)
        finally:
            os.close(self._lock_fd)
            self._released = True


@dataclass(frozen=True)
class FailOpenPidOwnership:
    """Test-only ownership resolution when advisory PID locking is skipped."""

    error: str

    def release(self) -> None:
        """Match the ownership contract when no PID-file claim exists."""


PidOwnershipResolution = PidFileClaim | FailOpenPidOwnership


def _lock_file(lock_fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows only
        os.lseek(lock_fd, 0, os.SEEK_SET)
        _msvcrt.locking(lock_fd, _msvcrt.LK_NBLCK, 1)
        return
    raise OSError("no supported file locking implementation")  # pragma: no cover


def _unlock_file(lock_fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
    elif _msvcrt is not None:  # pragma: no cover - Windows only
        os.lseek(lock_fd, 0, os.SEEK_SET)
        _msvcrt.locking(lock_fd, _msvcrt.LK_UNLCK, 1)


def _read_pid(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _open_lock(lock_path: Path) -> int:
    try:
        return os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SingletonOpenError(str(exc)) from exc


def _acquire_lock(lock_fd: int) -> bool:
    try:
        _lock_file(lock_fd)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise SingletonLockError(str(exc)) from exc
    return True


def _reservation_view(payload: dict[str, object] | None) -> ServiceReservationView | None:
    if not isinstance(payload, dict):
        return None
    backend = payload.get("backend")
    nonce = payload.get("nonce")
    nonce_path = payload.get("nonce_path")
    issued_at = payload.get("issued_at")
    boot_id = payload.get("boot_id")
    if not (
        isinstance(backend, str)
        and isinstance(nonce, str)
        and isinstance(nonce_path, str)
        and isinstance(boot_id, str)
        and isinstance(issued_at, (int, float, str))
    ):
        return None
    try:
        issued = float(issued_at)
    except (TypeError, ValueError):
        return None
    return ServiceReservationView(
        backend=backend,
        nonce=nonce,
        nonce_path=nonce_path,
        issued_at=issued,
        boot_id=boot_id,
    )


def _dict_reservation(record: dict[str, object] | None) -> dict[str, object] | None:
    if record is None:
        return None
    payload = record.get("reservation")
    return payload if isinstance(payload, dict) else None


def _probe_from_record(
    record: dict[str, object] | None,
    *,
    lock_held: bool,
    empty: bool,
) -> SingletonProbe:
    if empty:
        return SingletonProbe(state=ProbeState.TRANSITIONING if lock_held else ProbeState.ABSENT)
    if record is None:
        return SingletonProbe(state=ProbeState.TRANSITIONING)
    state = record.get("state")
    role = record.get("role")
    pid = record.get("pid")
    generation = record.get("generation")
    boot_id = record.get("boot_id")
    reservation_payload = _dict_reservation(record)
    reservation = _reservation_view(reservation_payload)
    parsed_pid = pid if isinstance(pid, int) else None
    parsed_generation = generation if isinstance(generation, int) else None
    parsed_boot = boot_id if isinstance(boot_id, str) else None
    if state == TRANSITIONING_STATE or role == TRANSITIONING_STATE:
        return SingletonProbe(
            state=ProbeState.TRANSITIONING,
            pid=parsed_pid,
            role=TRANSITIONING_STATE,
            generation=parsed_generation,
            boot_id=parsed_boot,
        )
    if reservation_payload is not None and reservation_is_live(reservation_payload):
        return SingletonProbe(
            state=ProbeState.LIVE_RESERVATION,
            pid=parsed_pid,
            reservation=reservation,
            generation=parsed_generation,
            boot_id=parsed_boot,
        )
    if reservation_payload is not None:
        return SingletonProbe(
            state=ProbeState.STALE_RESERVATION,
            pid=parsed_pid,
            reservation=reservation,
            generation=parsed_generation,
            boot_id=parsed_boot,
        )
    if lock_held:
        if role == "maintenance":
            return SingletonProbe(
                state=ProbeState.MAINTENANCE,
                pid=parsed_pid,
                role="maintenance",
                generation=parsed_generation,
                boot_id=parsed_boot,
            )
        if role == "daemon":
            return SingletonProbe(
                state=ProbeState.DAEMON,
                pid=parsed_pid,
                role="daemon",
                generation=parsed_generation,
                boot_id=parsed_boot,
            )
        return SingletonProbe(state=ProbeState.TRANSITIONING, pid=parsed_pid)
    return SingletonProbe(state=ProbeState.ABSENT)


def _clear_stale_reservation(record: dict[str, object] | None) -> dict[str, object] | None:
    reservation = record.get("reservation") if record else None
    if not isinstance(reservation, dict):
        return record
    if reservation_is_live(reservation):
        return record
    view = _reservation_view(reservation)
    if view is not None:
        unlink_matching_nonce(Path(view.nonce_path), view.nonce)
    if record is None:
        return None
    cleaned = dict(record)
    cleaned["reservation"] = None
    return cleaned


def _write_role_record(
    lock_fd: int,
    *,
    role: str,
    generation: int,
    reservation: dict[str, object] | None = None,
    ack: dict[str, object] | None = None,
) -> None:
    write_transitioning_record(lock_fd, generation)
    write_record(
        lock_fd,
        {
            "version": RECORD_VERSION,
            "state": role,
            "role": role,
            "pid": os.getpid(),
            "boot_id": current_boot_id(),
            "generation": generation,
            "reservation": reservation,
            "ack": ack,
        },
    )


def _write_pid_file(pid_file: Path) -> None:
    pid_fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(pid_fd, str(os.getpid()).encode())
        os.fsync(pid_fd)
    finally:
        os.close(pid_fd)


def _close_failed(lock_fd: int) -> None:
    try:
        truncate_record(lock_fd)
        _unlock_file(lock_fd)
    finally:
        os.close(lock_fd)


def claim_pid_file(pid_file: Path, *, role: str = "daemon") -> PidFileClaim | None:
    """Claim ``pid_file`` with ``role``, or return ``None`` on live contention."""
    if role not in {"daemon", "maintenance"}:
        raise SingletonRecordError(f"unsupported claimant role {role!r}")
    lock_path = pid_file.with_name(f"{pid_file.name}.lock")
    lock_fd = _open_lock(lock_path)
    try:
        if not _acquire_lock(lock_fd):
            os.close(lock_fd)
            return None
        record = _clear_stale_reservation(read_record_from_fd(lock_fd))
        if reservation_is_live(_dict_reservation(record)):
            _unlock_file(lock_fd)
            os.close(lock_fd)
            return None
        stored_pid = _read_pid(pid_file)
        if stored_pid is not None and stored_pid != os.getpid() and _pid_is_alive(stored_pid):
            _unlock_file(lock_fd)
            os.close(lock_fd)
            return None
        generation = next_generation(record)
        _write_role_record(lock_fd, role=role, generation=generation)
        _write_pid_file(pid_file)
    except SingletonError:
        _close_failed(lock_fd)
        raise
    except OSError as exc:
        _close_failed(lock_fd)
        raise SingletonRecordError(str(exc)) from exc
    except BaseException:
        _close_failed(lock_fd)
        raise
    return PidFileClaim(lock_path, lock_fd, role=role, generation=generation)


def probe_daemon_lock(pid_file: Path) -> SingletonProbe:
    """Probe the singleton without disturbing a live owner."""
    lock_path = pid_file.with_name(f"{pid_file.name}.lock")
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return SingletonProbe(state=ProbeState.ABSENT)
    try:
        try:
            _lock_file(lock_fd)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raw_empty = False
                try:
                    raw_empty = lock_path.stat().st_size == 0
                except OSError:
                    raw_empty = True
                return _probe_from_record(
                    read_record_from_path(lock_path),
                    lock_held=True,
                    empty=raw_empty,
                )
            raise SingletonLockError(str(exc)) from exc
        raw = b""
        try:
            os.lseek(lock_fd, 0, os.SEEK_SET)
            raw = os.read(lock_fd, 1 << 16)
        except OSError:
            raw = b""
        record = decode_or_none(raw)
        _unlock_file(lock_fd)
        return _probe_from_record(record, lock_held=False, empty=not raw.strip())
    finally:
        os.close(lock_fd)


def decode_or_none(raw: bytes) -> dict[str, object] | None:
    from gobby.runner_pid_record import decode_record

    if not raw.strip():
        return None
    return decode_record(raw)


def adopt_inherited_claim(
    pid_file: Path,
    env: Mapping[str, str] | None = None,
) -> PidFileClaim | None:
    """Adopt a lock descriptor inherited from a parent ``gobby start``."""
    environ = os.environ if env is None else env
    raw_fd = environ.get(INHERITED_LOCK_FD_ENV)
    if not raw_fd:
        return None
    try:
        lock_fd = int(raw_fd)
    except ValueError:
        return None
    lock_path = pid_file.with_name(f"{pid_file.name}.lock")
    record = read_record_from_fd(lock_fd)
    generation = next_generation(record)
    try:
        _write_role_record(lock_fd, role="daemon", generation=generation)
        _write_pid_file(pid_file)
    except (SingletonError, OSError) as exc:
        raise SingletonRecordError(str(exc)) from exc
    return PidFileClaim(lock_path, lock_fd, role="daemon", generation=generation)


def reserve_service_start(pid_file: Path, *, backend: str) -> ServiceReservationView:
    """Mint a one-shot nonce reservation and release the flock."""
    lock_path = pid_file.with_name(f"{pid_file.name}.lock")
    lock_fd = _open_lock(lock_path)
    nonce_path = service_nonce_path(pid_file)
    try:
        if not _acquire_lock(lock_fd):
            os.close(lock_fd)
            raise SingletonReservationError("singleton lock is held")
        record = _clear_stale_reservation(read_record_from_fd(lock_fd))
        if reservation_is_live(_dict_reservation(record)):
            _unlock_file(lock_fd)
            os.close(lock_fd)
            raise SingletonReservationError("a service start reservation is already live")
        stored_pid = _read_pid(pid_file)
        if stored_pid is not None and stored_pid != os.getpid() and _pid_is_alive(stored_pid):
            _unlock_file(lock_fd)
            os.close(lock_fd)
            raise SingletonReservationError("a live process still owns the pid file")
        nonce = secrets.token_hex(16)
        generation = next_generation(record)
        issued_at = current_time()
        boot = current_boot_id()
        reservation = {
            "backend": backend,
            "nonce": nonce,
            "nonce_path": str(nonce_path),
            "issued_at": issued_at,
            "boot_id": boot,
            "age_bound_seconds": RESERVATION_AGE_BOUND_SECONDS,
        }
        write_transitioning_record(lock_fd, generation)
        create_service_nonce_file(nonce_path, nonce)
        write_record(
            lock_fd,
            {
                "version": RECORD_VERSION,
                "state": "reservation",
                "role": None,
                "pid": os.getpid(),
                "boot_id": boot,
                "generation": generation,
                "reservation": reservation,
                "ack": None,
            },
        )
    except SingletonReservationError:
        raise
    except (SingletonError, NonceError, OSError) as exc:
        _close_failed(lock_fd)
        raise SingletonReservationError(str(exc)) from exc
    except BaseException:
        _close_failed(lock_fd)
        raise
    _unlock_file(lock_fd)
    os.close(lock_fd)
    return ServiceReservationView(
        backend=backend,
        nonce=nonce,
        nonce_path=str(nonce_path),
        issued_at=issued_at,
        boot_id=boot,
    )


def convert_or_acquire_service_claim(pid_file: Path) -> PidFileClaim:
    """Marked service runner: convert a matching nonce or direct-acquire."""
    lock_path = pid_file.with_name(f"{pid_file.name}.lock")
    lock_fd = _open_lock(lock_path)
    try:
        if not _acquire_lock(lock_fd):
            os.close(lock_fd)
            raise SingletonReservationError("singleton lock is held")
        record = _clear_stale_reservation(read_record_from_fd(lock_fd))
        reservation_payload = _dict_reservation(record)
        live = reservation_is_live(reservation_payload)
        nonce_path = os.environ.get(SERVICE_NONCE_ENV, "")
        nonce_exists = bool(nonce_path) and Path(nonce_path).is_file()
        if live:
            if not reservation_payload:
                _unlock_file(lock_fd)
                os.close(lock_fd)
                raise SingletonReservationError("live reservation is malformed")
            expected = str(reservation_payload.get("nonce", ""))
            expected_path = str(reservation_payload.get("nonce_path", ""))
            if not nonce_path or Path(nonce_path) != Path(expected_path):
                _unlock_file(lock_fd)
                os.close(lock_fd)
                raise SingletonReservationError("service nonce path does not match reservation")
            try:
                consume_service_nonce_file(Path(nonce_path), expected)
            except (NonceError, PermissionError, OSError) as exc:
                _unlock_file(lock_fd)
                os.close(lock_fd)
                raise SingletonReservationError(str(exc)) from exc
            generation = next_generation(record)
            _write_role_record(
                lock_fd,
                role="daemon",
                generation=generation,
                ack={"status": "converted", "pid": os.getpid()},
            )
            _write_pid_file(pid_file)
            return PidFileClaim(lock_path, lock_fd, role="daemon", generation=generation)
        if nonce_exists:
            _unlock_file(lock_fd)
            os.close(lock_fd)
            raise SingletonReservationError("service nonce replay refused")
        generation = next_generation(record)
        _write_role_record(lock_fd, role="daemon", generation=generation)
        _write_pid_file(pid_file)
        return PidFileClaim(lock_path, lock_fd, role="daemon", generation=generation)
    except SingletonReservationError:
        raise
    except (SingletonError, OSError) as exc:
        _close_failed(lock_fd)
        raise SingletonReservationError(str(exc)) from exc


def cancel_service_reservation(pid_file: Path) -> SingletonProbe:
    """Atomically clear a live reservation and its matching nonce."""
    lock_path = pid_file.with_name(f"{pid_file.name}.lock")
    lock_fd = _open_lock(lock_path)
    try:
        if not _acquire_lock(lock_fd):
            os.close(lock_fd)
            return probe_daemon_lock(pid_file)
        record = read_record_from_fd(lock_fd)
        reservation = _reservation_view(
            record.get("reservation")
            if record and isinstance(record.get("reservation"), dict)
            else None
        )
        if reservation is not None:
            unlink_matching_nonce(Path(reservation.nonce_path), reservation.nonce)
        truncate_record(lock_fd)
        _unlock_file(lock_fd)
    except (SingletonError, OSError):
        _close_failed(lock_fd)
        raise
    else:
        os.close(lock_fd)
    return SingletonProbe(state=ProbeState.ABSENT)


def prepare_commanded_service_start(
    pid_file: Path | None = None,
    *,
    backend: str | None = None,
) -> ServiceReservationView:
    """Public nonce-minting admission path for commanded service starts."""
    import sys

    from gobby.paths import get_gobby_home

    target = pid_file or (get_gobby_home() / "gobby.pid")
    chosen = backend
    if chosen is None:
        if sys.platform == "darwin":
            chosen = "launchd"
        elif sys.platform == "linux":
            chosen = "systemd"
        elif sys.platform.startswith("win"):
            chosen = "windows"
        else:
            raise SingletonReservationError(f"unsupported platform: {sys.platform}")
    return reserve_service_start(target, backend=chosen)


__all__ = [
    "INHERITED_LOCK_FD_ENV",
    "RESERVATION_AGE_BOUND_SECONDS",
    "SERVICE_LAUNCH_ENV",
    "SERVICE_NONCE_ENV",
    "FailOpenPidOwnership",
    "PidFileClaim",
    "PidOwnershipResolution",
    "ProbeState",
    "ServiceReservationView",
    "SingletonError",
    "SingletonFsyncError",
    "SingletonLockError",
    "SingletonOpenError",
    "SingletonRecordError",
    "SingletonReservationError",
    "SingletonProbe",
    "adopt_inherited_claim",
    "cancel_service_reservation",
    "claim_pid_file",
    "convert_or_acquire_service_claim",
    "prepare_commanded_service_start",
    "probe_daemon_lock",
    "reserve_service_start",
    "service_nonce_path",
]
