"""One-shot service-start nonce helpers for the singleton reservation."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import cast


class NonceError(Exception):
    """Nonce create or consume failed."""


class _Kernel32Adapter:
    """Typed boundary around the dynamic kernel32 functions used for nonce creation."""

    def __init__(self) -> None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise RuntimeError("kernel32 is available only on Windows")
        kernel32 = win_dll("kernel32", use_last_error=True)

        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        self._create_file = cast(Callable[..., int | None], create_file)

        write_file = kernel32.WriteFile
        write_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            wintypes.LPDWORD,
            wintypes.LPVOID,
        ]
        write_file.restype = wintypes.BOOL
        self._write_file = cast(Callable[..., int], write_file)

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        self._close_handle = cast(Callable[[int], int], close_handle)

    def create_file(self, path: str) -> int | None:  # pragma: no cover - Windows only
        return self._create_file(
            path,
            0x40000000,
            0,
            None,
            1,
            0x80,
            None,
        )

    def write_file(self, handle: int, nonce: bytes) -> bool:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        written = wintypes.DWORD()
        return bool(
            self._write_file(
                handle,
                nonce,
                len(nonce),
                ctypes.byref(written),
                None,
            )
        )

    def close_handle(self, handle: int) -> None:  # pragma: no cover - Windows only
        self._close_handle(handle)


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

    kernel32 = _Kernel32Adapter()
    handle = kernel32.create_file(path)
    if handle == ctypes.c_void_p(-1).value:
        raise NonceError(f"CREATE_NEW failed for {path}")
    if handle is None:
        raise NonceError(f"CreateFileW returned a null handle for {path}")
    try:
        if not kernel32.write_file(handle, nonce):
            raise NonceError(f"WriteFile failed for {path}")
    finally:
        kernel32.close_handle(handle)
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
