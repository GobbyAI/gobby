"""Bounded, daemon-private paths for SRT's shared and internal Unix sockets."""

import hashlib
import os
import stat
from pathlib import Path

from gobby.paths import get_gobby_home

# Reserve 40 bytes for SRT's PID/sequence name, separator, and terminating NUL
# in macOS's 104-byte sun_path. The returned directory is always canonical.
_SOCKET_DIRECTORY_BUDGET = 63


def private_srt_socket_roots(gobby_home: Path | None = None) -> tuple[Path, Path]:
    """Return both private roots, including other homes' UID-scoped sockets."""
    return (
        (gobby_home if gobby_home is not None else get_gobby_home()) / "runtime" / "srt-sock",
        Path("/tmp") / f"gobby-srt-{os.getuid()}",
    )


def srt_socket_directory(gobby_home: Path | None = None) -> Path:
    """Select a stable bounded directory without creating policy-side state."""
    home = gobby_home if gobby_home is not None else get_gobby_home()
    home_root, short_root = private_srt_socket_roots(home)
    home_root = home_root.parent.resolve() / home_root.name
    if len(os.fsencode(home_root)) <= _SOCKET_DIRECTORY_BUDGET:
        return home_root
    home_key = hashlib.sha256(os.fsencode(home.resolve())).hexdigest()[:16]
    return short_root.parent.resolve() / short_root.name / home_key


def _ensure_private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"Unsafe SRT socket directory: {directory}")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def srt_mux_tmpdir() -> Path:
    """Create the private short root used by the host runner and shared broker."""
    directory = srt_socket_directory()
    _, short_root = private_srt_socket_roots()
    short_root = short_root.parent.resolve() / short_root.name
    if directory.is_relative_to(short_root):
        _ensure_private_directory(short_root)
    _ensure_private_directory(directory)
    return directory
