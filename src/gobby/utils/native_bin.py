"""Helpers for resolving local Gobby-managed native binaries."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def native_bin_name(name: str) -> str:
    """Return the platform-specific executable name for a native binary."""
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def native_bin_dir() -> Path:
    """Return the managed native binary directory."""
    return Path.home() / ".gobby" / "bin"


def local_native_bin_path(name: str) -> Path:
    """Return the preferred ``~/.gobby/bin`` path for a native binary."""
    return native_bin_dir() / native_bin_name(name)


def is_native_bin_usable(path: Path) -> bool:
    """Return whether a local native binary path can be executed."""
    if sys.platform == "win32":
        return path.is_file()
    return path.is_file() and os.access(path, os.X_OK)


def _resolve_path_native_bin(name: str) -> str | None:
    resolved = shutil.which(name)
    if resolved:
        return resolved

    platform_name = native_bin_name(name)
    if platform_name != name:
        return shutil.which(platform_name)

    return None


def resolve_native_bin(name: str) -> str | None:
    """Resolve a native binary, preferring Gobby's managed bin before PATH."""
    local_path = local_native_bin_path(name)
    if is_native_bin_usable(local_path):
        return str(local_path)

    return _resolve_path_native_bin(name)


def resolve_native_bin_or_default(name: str) -> str:
    """Resolve a native binary, falling back to the bare command name."""
    return resolve_native_bin(name) or name
