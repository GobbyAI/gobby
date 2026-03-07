"""Security checks for code indexing.

Validates paths, detects binary files, and filters sensitive content.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

# Extensions that likely contain secrets
_SECRET_EXTENSIONS = frozenset(
    {
        ".env",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
        ".secret",
    }
)

_SECRET_PREFIXES = frozenset(
    {
        "credentials",
        ".env",
        "id_rsa",
        "id_ed25519",
        "token",
    }
)

_SECRET_SUBSTRINGS = frozenset(
    {
        "api_key",
        "apikey",
        "_secret.",
        "_token.",
    }
)


def validate_path(path: Path, root: Path) -> bool:
    """Check that path resolves within root (prevents traversal)."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        return resolved.is_relative_to(root_resolved)
    except (OSError, ValueError):
        return False


def is_symlink_safe(path: Path, root: Path) -> bool:
    """Check that symlink target is still within root."""
    try:
        if not path.is_symlink():
            return True
        resolved = path.resolve()
        return resolved.is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def is_binary(path: Path, check_bytes: int = 8192) -> bool:
    """Check if file appears to be binary (has null bytes in first N bytes)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(check_bytes)
        return b"\x00" in chunk
    except OSError:
        return True


def should_exclude(path: Path, patterns: list[str]) -> bool:
    """Check if path matches any exclusion pattern."""
    path_str = str(path)
    for pattern in patterns:
        # Check each component of the path
        for part in path.parts:
            if fnmatch.fnmatch(part, pattern):
                return True
        # Also check the full path
        if fnmatch.fnmatch(path_str, f"*{pattern}*"):
            return True
    return False


def has_secret_extension(path: Path) -> bool:
    """Check if file extension suggests secret content."""
    name = path.name.lower()
    suffix = path.suffix.lower()

    if suffix in _SECRET_EXTENSIONS:
        return True

    for prefix in _SECRET_PREFIXES:
        if name.startswith(prefix):
            return True

    for substring in _SECRET_SUBSTRINGS:
        if substring in name:
            return True

    return False
