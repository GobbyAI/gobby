"""Loopback recovery credential file boundary."""

import os
import secrets
import stat
from pathlib import Path

from gobby.utils.local_token import daemon_bootstrap_path

BREAK_GLASS_FILENAME = "break_glass"
BREAK_GLASS_HEADER = "X-Gobby-Break-Glass"


def break_glass_path(gobby_home: Path | None = None) -> Path:
    return (
        gobby_home if gobby_home is not None else daemon_bootstrap_path().parent
    ) / BREAK_GLASS_FILENAME


def ensure_break_glass_credential(gobby_home: Path) -> None:
    """Create once with owner-only permissions; preserve a concurrent winner."""
    try:
        fd = os.open(break_glass_path(gobby_home), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(32))


def break_glass_matches(path: Path, presented: str) -> bool:
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
                return False
            credential = stream.read().strip()
    except (OSError, UnicodeError):
        return False
    return bool(credential) and secrets.compare_digest(credential.encode(), presented.encode())
