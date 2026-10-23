"""Cargo build variables that every agent launch path applies."""

from __future__ import annotations

from pathlib import Path


def cargo_environment(cache_dir: Path, socket: Path | None) -> dict[str, str]:
    """Return the sccache and Cargo job-limit variables for one launch."""
    raise NotImplementedError
