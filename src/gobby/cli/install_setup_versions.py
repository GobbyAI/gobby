"""Version-floor helpers for install-time native binary installers."""

from __future__ import annotations

from gobby.install.bin_freshness_models import is_at_least_version
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS


def managed_version_satisfies_pin(name: str, installed_version: str | None) -> bool:
    """Return whether an installed managed helper version satisfies its floor."""
    floor = MANAGED_BIN_VERSION_PINS[name]
    return is_at_least_version(installed_version, floor)
