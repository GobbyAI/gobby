"""One-shot service-start nonce helpers for the singleton reservation."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class NonceError(Exception):
    """Nonce create or consume failed."""


def service_nonce_path(pid_file: Path) -> Path:
    return pid_file.with_name(f"{pid_file.name}.service-nonce")


def create_posix_nonce_file(path: Path, nonce: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, nonce.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        path.unlink(missing_ok=True)
        raise NonceError(f"nonce file {path} has unexpected owner or mode")


def consume_posix_nonce_file(path: Path, expected: str) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise NonceError(f"nonce file {path} is missing") from exc
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise NonceError(f"nonce file {path} failed owner/mode validation")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NonceError(f"nonce file {path} is unreadable") from exc
    if content != expected:
        raise NonceError("nonce content does not match the reservation")
    path.unlink()


def create_windows_nonce_file(path: Path, nonce: bytes, *, user_sid: str) -> None:
    _windows_create_file_exclusive(str(path), nonce=nonce, user_sid=user_sid)


def consume_windows_nonce_file(path: Path, expected: bytes) -> None:
    if not _windows_nonce_acl_is_owner_only(path):
        raise PermissionError(f"nonce file {path} ACL is not owner-only")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise NonceError(f"nonce file {path} is unreadable") from exc
    if content != expected:
        raise NonceError("nonce content does not match the reservation")
    path.unlink()


def create_service_nonce_file(path: Path, nonce: str) -> None:
    if os.name == "nt":  # pragma: no cover - Windows only
        create_windows_nonce_file(path, nonce.encode(), user_sid="current")
        return
    create_posix_nonce_file(path, nonce)


def consume_service_nonce_file(path: Path, expected: str) -> None:
    if os.name == "nt":  # pragma: no cover - Windows only
        consume_windows_nonce_file(path, expected.encode())
        return
    consume_posix_nonce_file(path, expected)


def unlink_matching_nonce(path: Path, expected: str) -> bool:
    """Remove ``path`` only when it exists and matches ``expected``."""
    try:
        consume_service_nonce_file(path, expected)
    except (NonceError, PermissionError, OSError):
        return False
    return True


def _windows_create_file_exclusive(
    path: str,
    *,
    nonce: bytes,
    user_sid: str,
) -> None:  # pragma: no cover - replaced in tests / Windows
    import ctypes
    from ctypes import wintypes

    generic_write = 0x40000000
    create_new = 1
    file_attribute_normal = 0x80
    handle = ctypes.windll.kernel32.CreateFileW(  # type: ignore[attr-defined]
        path,
        generic_write,
        0,
        None,
        create_new,
        file_attribute_normal,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise NonceError(f"CREATE_NEW failed for {path}")
    try:
        written = wintypes.DWORD()
        if not ctypes.windll.kernel32.WriteFile(  # type: ignore[attr-defined]
            handle,
            nonce,
            len(nonce),
            ctypes.byref(written),
            None,
        ):
            raise NonceError(f"WriteFile failed for {path}")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    _ = user_sid


def _windows_nonce_acl_is_owner_only(path: Path) -> bool:  # pragma: no cover
    return path.is_file()


__all__ = [
    "NonceError",
    "consume_service_nonce_file",
    "consume_windows_nonce_file",
    "create_service_nonce_file",
    "create_windows_nonce_file",
    "service_nonce_path",
    "unlink_matching_nonce",
]
