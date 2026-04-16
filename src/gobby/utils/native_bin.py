"""Helpers for resolving local Gobby-managed native binaries."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def native_bin_name(name: str) -> str:
    """Return the platform-specific executable name for a native binary."""
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def local_native_bin_path(name: str) -> Path:
    """Return the preferred ``~/.gobby/bin`` path for a native binary."""
    return Path.home() / ".gobby" / "bin" / native_bin_name(name)


def resolve_native_bin(name: str) -> str | None:
    """Resolve a native binary, preferring ``~/.gobby/bin`` over ``PATH``."""
    local_path = local_native_bin_path(name)
    if local_path.exists():
        return str(local_path)

    resolved = shutil.which(name)
    if resolved:
        return resolved

    platform_name = native_bin_name(name)
    if platform_name != name:
        return shutil.which(platform_name)

    return None


def resolve_native_bin_or_default(name: str) -> str:
    """Resolve a native binary, falling back to the bare command name."""
    return resolve_native_bin(name) or name
