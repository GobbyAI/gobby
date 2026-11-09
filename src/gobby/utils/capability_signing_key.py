"""Daemon-owned capability signing authority, independent of client machine credentials."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

CAPABILITY_SIGNING_KEY_FILENAME = "capability_signing_key"


def read_capability_signing_key(path: Path) -> bytes | None:
    """Read the same owner-only regular inode we validate; never follow a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid()
            ):
                return None
            key = stream.read(33)
    except OSError:
        return None
    return key if len(key) == 32 else None


def ensure_capability_signing_key(path: Path) -> bytes | None:
    """Publish a complete random key once, preserving an existing concurrent winner.

    The temporary filename is covered by the credential-read block as well. Existing
    unsafe or damaged files fail closed rather than silently rotating live credentials.
    """
    if not os.path.lexists(path):
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{CAPABILITY_SIGNING_KEY_FILENAME}-", dir=path.parent
            ) as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
                try:
                    os.link(stream.name, path)
                except FileExistsError:
                    pass
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError:
            return None
    return read_capability_signing_key(path)
