"""Where agent Cargo build folders live.

The build root is a restart-required setting. The daemon installs its configured value
once at startup, so every cleanup, sandbox grant and launch in that process reads the
same canonical directory. Cargo homes stay under the Gobby cache: they hold registries,
not build output.
"""

from __future__ import annotations

from pathlib import Path

from gobby.agents.constants import agent_home_cache_root
from gobby.config.agent_build import validate_build_root

_configured: Path | None = None


def default_build_root() -> Path:
    """Return the Gobby cache, where build folders have always lived."""
    return agent_home_cache_root()


def configure_build_root(value: str | None) -> Path:
    """Install the configured build root and return it. Empty restores the default.

    The same validator gates the config write and the restart preflight, so every
    place that accepts the setting refuses the same values. A missing root fails closed
    rather than letting build folders fall onto the boot disk while the volume is
    unmounted.
    """
    global _configured
    canonical = validate_build_root(value)
    _configured = canonical
    return canonical if canonical is not None else default_build_root()


def agent_build_root() -> Path:
    """Return the root build folders are written under in this process."""
    if _configured is not None:
        # A dedicated volume can disappear after startup; never recreate its root.
        validate_build_root(str(_configured))
    return _configured if _configured is not None else default_build_root()
