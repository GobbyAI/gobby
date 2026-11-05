"""Cargo build variables that every agent launch path applies."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

# macOS sun_path holds 104 bytes including the terminator; Linux allows 107.
_SOCKET_PATH_LIMIT = 103
# Cache sizing: the cap stays at sccache's default of 10 GiB and is not set here. The Rust
# mirror of this environment (crates/gdaemon RunLayout) and its corpus pin the exact key set,
# so a new key needs a crate change. Measured on sccache 0.19.0: one gobby-core dev dependency
# closure occupies about 119 MiB, so the default holds about 86 such closures before
# least-recently-used eviction.
_RUSTC_WRAPPER = "rustc-wrapper"
# Owner-execute only: the launch runs the shim, and no one may rewrite it in place.
_SHIM_MODE = 0o500


def cargo_environment(cache_dir: Path, socket: Path | None, shim_dir: Path) -> dict[str, str]:
    """Return the sccache and Cargo job-limit variables for one launch.

    RUSTC_WRAPPER names a shim in shim_dir, not sccache itself. sccache keys each
    Rust compile on every CARGO_* variable, and Cargo sets CARGO_TARGET_DIR per checkout,
    so a second checkout misses every Rust unit the first one built. The shim drops the
    two target variables for the compiler only; Cargo still writes to its own target.

    shim_dir must be readable and executable by the launch but not writable by it: the
    shim is the program every rustc invocation runs.

    Without sccache, a usable socket, or a writable shim, the sccache variables are blank
    rather than absent: launch callers merge this over an inherited environment, and an
    absent key would keep a value from another trust domain. Cargo treats an empty
    RUSTC_WRAPPER as no wrapper.
    """
    # Keep the PATH spelling: a resolved Homebrew Cellar path breaks on upgrade.
    sccache = shutil.which("sccache")
    if sccache is None or socket is None or len(os.fsencode(socket)) > _SOCKET_PATH_LIMIT:
        return _blank_sccache()
    shim = _write_rustc_wrapper(sccache, shim_dir)
    if shim is None:
        return _blank_sccache()
    return {
        "CARGO_BUILD_JOBS": "4",
        "RUSTC_WRAPPER": str(shim),
        "SCCACHE_DIR": str(cache_dir),
        "SCCACHE_SERVER_UDS": str(socket),
    }


def _blank_sccache() -> dict[str, str]:
    return {
        "CARGO_BUILD_JOBS": "4",
        "RUSTC_WRAPPER": "",
        "SCCACHE_DIR": "",
        "SCCACHE_SERVER_UDS": "",
    }


def _write_rustc_wrapper(sccache: str, directory: Path) -> Path | None:
    """Publish the executable shim into directory, or return None if it cannot be published.

    A current shim is reused as is. The shim is written and its mode set on one descriptor,
    then renamed into place, so no path is changed after it is published.
    """
    shim = directory / _RUSTC_WRAPPER
    script = (
        "#!/bin/sh\n"
        "unset CARGO_TARGET_DIR CARGO_BUILD_TARGET_DIR\n"
        f'exec {shlex.quote(sccache)} "$@"\n'
    )
    try:
        if _is_current_shim(shim, script):
            return shim
        directory.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(dir=directory, prefix=f".{_RUSTC_WRAPPER}-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            os.fchmod(handle.fileno(), _SHIM_MODE)
        os.replace(staged, shim)
    except OSError:
        return None
    return shim


def _is_current_shim(shim: Path, script: str) -> bool:
    """Return whether shim is a regular, daemon-owned, owner-only file holding script.

    O_NOFOLLOW refuses a link at the final name. The owner, mode, and bytes are read from
    the same descriptor, so the bytes checked are the bytes a launch executes.
    """
    try:
        fd = os.open(shim, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as handle:
        status = os.fstat(handle.fileno())
        return (
            stat.S_ISREG(status.st_mode)
            and status.st_uid == os.getuid()
            and stat.S_IMODE(status.st_mode) == _SHIM_MODE
            and handle.read() == script.encode("utf-8")
        )
