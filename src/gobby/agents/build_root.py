"""Where agent Cargo build folders live.

The build root is a restart-required setting. The daemon installs its configured value
once at startup, so every cleanup, sandbox grant and launch in that process reads the
same canonical directory. Cargo homes and sccache stay under the Gobby cache: they hold
registries and compiled objects, not build output.
"""

from __future__ import annotations

from pathlib import Path

from gobby.agents.constants import agent_home_cache_root
from gobby.utils.local_token import account_home

_configured: Path | None = None


def default_build_root() -> Path:
    """Return the Gobby cache, where build folders have always lived."""
    return agent_home_cache_root()


def configure_build_root(value: str | None) -> Path:
    """Install the configured build root and return it. Empty restores the default.

    The root must be an existing absolute directory spelled canonically, so a symlink
    never redirects a sandbox write grant. A missing root fails closed rather than
    letting build folders fall onto the boot disk while the volume is unmounted.
    """
    global _configured
    if not value:
        _configured = None
        return default_build_root()
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"agent build root must be absolute: {value}")
    if path.resolve(strict=False) != path:
        raise ValueError(f"agent build root must be canonical, with no symlink components: {value}")
    if not path.is_dir():
        raise ValueError(f"agent build root does not exist or is not a directory: {value}")
    # The passwd home is the account's real home; `$HOME` alone could name another directory.
    accounts = {Path.home(), account_home()}
    if path == Path(path.anchor) or path in accounts:
        raise ValueError(f"agent build root is too broad: {value}")
    _configured = path
    return path


def agent_build_root() -> Path:
    """Return the root build folders are written under in this process."""
    return _configured if _configured is not None else default_build_root()
