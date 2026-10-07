"""Loopback recovery credential file boundary."""

import os
import secrets
import stat
import string
import tempfile
from pathlib import Path

from gobby.utils.local_token import daemon_bootstrap_path

BREAK_GLASS_FILENAME = "break_glass"
BREAK_GLASS_HEADER = "X-Gobby-Break-Glass"


def break_glass_path(gobby_home: Path | None = None) -> Path:
    return (
        gobby_home if gobby_home is not None else daemon_bootstrap_path().parent
    ) / BREAK_GLASS_FILENAME


def break_glass_staging_path(gobby_home: Path | None = None) -> Path:
    return break_glass_path(gobby_home).parent / ".break_glass-staging"


def _validate_existing_credential(path: Path) -> None:
    credential = _read_credential(path)
    if (
        credential is None
        or len(credential) != 43
        or any(
            character not in string.ascii_letters + string.digits + "_-" for character in credential
        )
    ):
        raise ValueError(
            f"Invalid break-glass credential at {path}; remove the damaged file and restart "
            "to recreate it."
        )


def ensure_break_glass_credential(gobby_home: Path) -> None:
    """Publish a complete owner-only credential once; preserve a concurrent winner."""
    path = break_glass_path(gobby_home)
    if os.path.lexists(path):
        _validate_existing_credential(path)
        return
    staging = break_glass_staging_path(gobby_home)
    staging.mkdir(mode=0o700, exist_ok=True)
    directory_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(directory_fd)
        if metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
            raise PermissionError(f"Break-glass staging directory must be owner-only: {staging}")
    finally:
        os.close(directory_fd)
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=staging) as stream:
        stream.write(secrets.token_urlsafe(32))
        stream.flush()
        os.fsync(stream.fileno())
        try:
            os.link(stream.name, path)
        except FileExistsError:
            _validate_existing_credential(path)
        directory_fd = os.open(gobby_home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _read_credential(path: Path) -> str | None:
    """Read and validate the same inode, refusing symlinks and non-regular files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid()
            ):
                return None
            credential = stream.read().strip()
    except (OSError, UnicodeError):
        return None
    return credential


def break_glass_matches(path: Path, presented: str) -> bool:
    """Match only an owner-only regular credential inode."""
    credential = _read_credential(path)
    if not credential:
        return False
    return secrets.compare_digest(credential.encode(), presented.encode())
