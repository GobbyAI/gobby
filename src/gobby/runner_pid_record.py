"""Role-bearing singleton record encoding for the daemon lock sidecar."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

RECORD_VERSION = 1
RESERVATION_AGE_BOUND_SECONDS = 30
TRANSITIONING_STATE = "transitioning"


class SingletonRecordError(Exception):
    """Role-record encode, decode, or write failed."""


class SingletonFsyncError(SingletonRecordError):
    """fsync of the singleton record failed."""


@functools.lru_cache(maxsize=1)
def current_boot_id() -> str:
    """Return a boot/session identity that changes across reboot."""
    linux_boot = Path("/proc/sys/kernel/random/boot_id")
    if linux_boot.is_file():
        try:
            return linux_boot.read_text(encoding="utf-8").strip() or "boot:unknown"
        except OSError:
            pass
    if sys.platform == "darwin":
        try:
            result = subprocess.run(  # nosec B603 B607
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "boot:unknown"


def checksum_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def encode_record(record: dict[str, Any]) -> bytes:
    body = {key: value for key, value in record.items() if key != "checksum"}
    body["checksum"] = checksum_payload(body)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def decode_record(raw: bytes) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="strict").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    checksum = parsed.get("checksum")
    body = {key: value for key, value in parsed.items() if key != "checksum"}
    if not isinstance(checksum, str) or checksum != checksum_payload(body):
        return None
    return parsed


def write_record(lock_fd: int, record: dict[str, Any]) -> None:
    payload = encode_record(record)
    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, payload)
        os.fsync(lock_fd)
    except OSError as exc:
        raise SingletonFsyncError(str(exc)) from exc


def write_transitioning_record(
    lock_fd: int,
    generation: int,
    *,
    boot_id: str | None = None,
) -> None:
    write_record(
        lock_fd,
        {
            "version": RECORD_VERSION,
            "state": TRANSITIONING_STATE,
            "role": TRANSITIONING_STATE,
            "pid": os.getpid(),
            "boot_id": boot_id or current_boot_id(),
            "generation": generation,
            "reservation": None,
            "ack": None,
        },
    )


def read_record_from_fd(lock_fd: int) -> dict[str, Any] | None:
    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        raw = os.read(lock_fd, 1 << 16)
    except OSError:
        return None
    if not raw.strip():
        return None
    return decode_record(raw)


def read_record_from_path(lock_path: Path) -> dict[str, Any] | None:
    try:
        raw = lock_path.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    return decode_record(raw)


def current_time() -> float:
    return time.time()


def reservation_is_live(
    reservation: dict[str, object] | None,
    *,
    now: float | None = None,
    boot_id: str | None = None,
) -> bool:
    if not reservation:
        return False
    current = now if now is not None else current_time()
    expected_boot = boot_id if boot_id is not None else current_boot_id()
    if reservation.get("boot_id") != expected_boot:
        return False
    issued_raw = reservation.get("issued_at", 0)
    bound_raw = reservation.get("age_bound_seconds", RESERVATION_AGE_BOUND_SECONDS)
    if not isinstance(issued_raw, (int, float, str)):
        return False
    if not isinstance(bound_raw, (int, float, str)):
        return False
    try:
        issued = float(issued_raw)
        bound = int(bound_raw)
    except (TypeError, ValueError):
        return False
    return bool((current - issued) < bound)


def next_generation(record: dict[str, Any] | None) -> int:
    if not record:
        return 1
    try:
        return int(record.get("generation", 0)) + 1
    except (TypeError, ValueError):
        return 1


def truncate_record(lock_fd: int) -> None:
    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.ftruncate(lock_fd, 0)
    except OSError:
        pass


__all__ = [
    "RECORD_VERSION",
    "RESERVATION_AGE_BOUND_SECONDS",
    "TRANSITIONING_STATE",
    "SingletonFsyncError",
    "SingletonRecordError",
    "checksum_payload",
    "current_boot_id",
    "current_time",
    "decode_record",
    "encode_record",
    "next_generation",
    "read_record_from_fd",
    "read_record_from_path",
    "reservation_is_live",
    "truncate_record",
    "write_record",
    "write_transitioning_record",
]
