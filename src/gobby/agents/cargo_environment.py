"""Cargo build variables that every agent launch path applies."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# macOS sun_path holds 104 bytes including the terminator; Linux allows 107.
_SOCKET_PATH_LIMIT = 103


def cargo_environment(cache_dir: Path, socket: Path | None) -> dict[str, str]:
    """Return the sccache and Cargo job-limit variables for one launch.

    Without sccache or a usable socket, the sccache variables are blank rather than
    absent: launch callers merge this over an inherited environment, and an absent
    key would keep a value from another trust domain. Cargo treats an empty
    RUSTC_WRAPPER as no wrapper.
    """
    # Keep the PATH spelling: a resolved Homebrew Cellar path breaks on upgrade.
    wrapper = shutil.which("sccache")
    if wrapper is None or socket is None or len(os.fsencode(socket)) > _SOCKET_PATH_LIMIT:
        wrapper, cache, server = "", "", ""
    else:
        cache, server = str(cache_dir), str(socket)
    return {
        "CARGO_BUILD_JOBS": "4",
        "RUSTC_WRAPPER": wrapper,
        "SCCACHE_DIR": cache,
        "SCCACHE_SERVER_UDS": server,
    }
