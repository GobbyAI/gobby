"""WSL compatibility utilities for tmux on Windows.

Provides path conversion and platform detection so that
``TmuxSessionManager`` can transparently prefix commands with ``wsl``
when running on Windows.
"""

from __future__ import annotations

import platform
import re


def needs_wsl() -> bool:
    """Return ``True`` when running on Windows (tmux requires WSL)."""
    return platform.system() == "Windows"


def convert_windows_path_to_wsl(path: str) -> str:
    """Convert a Windows path to its WSL ``/mnt/`` equivalent.

    Handles absolute drive paths (``C:\\Users\\foo`` -> ``/mnt/c/Users/foo``)
    and UNC paths (``\\\\server\\share`` -> ``//server/share``).

    Examples::

        >>> convert_windows_path_to_wsl("C:\\\\Users\\\\foo")
        '/mnt/c/Users/foo'
        >>> convert_windows_path_to_wsl(r"\\server\\share")
        '//server/share'
        >>> convert_windows_path_to_wsl("/already/unix")
        '/already/unix'

    Args:
        path: A filesystem path (Windows or Unix).

    Returns:
        The path in WSL format.  If the path is already Unix-style it is
        returned unchanged.
    """
    if path.startswith(("\\\\", "//")):
        return f"//{path.lstrip('/\\').replace('\\', '/')}"

    # Match absolute drive paths like C:\ or D:/.
    m = re.match(r"^([A-Za-z]):([/\\].*)", path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return path
