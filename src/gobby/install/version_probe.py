"""Shared probe for managed native-binary versions.

Runs ``<binary> --version`` and returns the trailing version token. The
install-time per-binary probes, the bin-freshness inspector, and the
dependency dashboard all route through this single helper so the
probe/last-token logic lives in exactly one place.
"""

from __future__ import annotations

import logging
import subprocess  # nosec B404 # used only for fixed `<binary> --version` probes
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["probe_native_bin_version"]


def probe_native_bin_version(
    binary_path: Path | str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    logger: logging.Logger | None = None,
    label: str | None = None,
) -> str | None:
    """Return the trailing token of ``<binary> --version``, or ``None``.

    ``None`` is returned when the binary cannot be executed, exits non-zero, or
    prints no token. ``runner`` is injected so install-time callers can route
    through their module-level ``subprocess`` seam; when both ``logger`` and
    ``label`` are supplied, failures are logged as warnings (matching the
    install-time probes).
    """
    try:
        result = runner(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if logger is not None and label is not None:
            logger.warning("%s: failed running --version probe: %s", label, exc)
        return None

    if result.returncode != 0:
        if logger is not None and label is not None:
            logger.warning("%s: --version probe failed: %s", label, result.stderr.strip())
        return None

    output = (result.stdout or result.stderr).strip()
    parts = output.split()
    return parts[-1] if parts else None
