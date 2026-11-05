"""Cargo build variables that every agent launch path applies."""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from pathlib import Path

# macOS sun_path holds 104 bytes including the terminator; Linux allows 107.
_SOCKET_PATH_LIMIT = 103
# sccache's default cap, set explicitly so every launch path shares one documented value.
# Measured on sccache 0.19.0: one gobby-core dev dependency closure occupies about 119 MiB,
# so 10G holds about 86 such closures before least-recently-used eviction.
_CACHE_CAP = "10G"
_RUSTC_WRAPPER = "rustc-wrapper"


def cargo_environment(cache_dir: Path, socket: Path | None) -> dict[str, str]:
    """Return the sccache and Cargo job-limit variables for one launch.

    RUSTC_WRAPPER names a shim beside cache_dir, not sccache itself. sccache keys each
    Rust compile on every CARGO_* variable, and Cargo sets CARGO_TARGET_DIR per checkout,
    so a second checkout misses every Rust unit the first one built. The shim drops the
    two target variables for the compiler only; Cargo still writes to its own target.

    Without sccache, a usable socket, or a writable shim, the sccache variables are blank
    rather than absent: launch callers merge this over an inherited environment, and an
    absent key would keep a value from another trust domain. Cargo treats an empty
    RUSTC_WRAPPER as no wrapper.
    """
    # Keep the PATH spelling: a resolved Homebrew Cellar path breaks on upgrade.
    sccache = shutil.which("sccache")
    if sccache is None or socket is None or len(os.fsencode(socket)) > _SOCKET_PATH_LIMIT:
        return _blank_sccache()
    shim = _write_rustc_wrapper(sccache, cache_dir.parent)
    if shim is None:
        return _blank_sccache()
    return {
        "CARGO_BUILD_JOBS": "4",
        "RUSTC_WRAPPER": str(shim),
        "SCCACHE_CACHE_SIZE": _CACHE_CAP,
        "SCCACHE_DIR": str(cache_dir),
        "SCCACHE_SERVER_UDS": str(socket),
    }


def _blank_sccache() -> dict[str, str]:
    return {
        "CARGO_BUILD_JOBS": "4",
        "RUSTC_WRAPPER": "",
        "SCCACHE_CACHE_SIZE": "",
        "SCCACHE_DIR": "",
        "SCCACHE_SERVER_UDS": "",
    }


def _write_rustc_wrapper(sccache: str, directory: Path) -> Path | None:
    """Write the executable shim into directory, or return None if it cannot be written.

    The shim is replaced atomically and left untouched when its content is current.
    """
    shim = directory / _RUSTC_WRAPPER
    script = (
        "#!/bin/sh\n"
        "unset CARGO_TARGET_DIR CARGO_BUILD_TARGET_DIR\n"
        f'exec {shlex.quote(sccache)} "$@"\n'
    )
    try:
        if shim.is_file() and shim.read_text(encoding="utf-8") == script:
            return shim
        directory.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(dir=directory, prefix=f".{_RUSTC_WRAPPER}-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(staged, 0o500)
        os.replace(staged, shim)
    except OSError:
        return None
    return shim
