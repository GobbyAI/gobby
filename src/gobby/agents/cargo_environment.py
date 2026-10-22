"""Project-scoped Cargo target and Cargo home resolution.

Phase 1 stub: signatures only. Phase 2 implements the single project target under
the project UUID namespace and the one Cargo home every launch path shares.
"""

from __future__ import annotations

from pathlib import Path


def project_cargo_target_dir(project_id: str, project_name: str) -> Path:
    """Return the Cargo target shared by every checkout of one project.

    The project UUID is the parent namespace and the leaf is the sanitized
    ``cargo-target-<project_name>``, so equal names in different projects never
    collide.
    """
    raise NotImplementedError


def project_cargo_home() -> Path:
    """Return the Cargo home that every launch path for a project uses."""
    raise NotImplementedError


def cargo_build_env(project_id: str, project_name: str) -> dict[str, str]:
    """Return the ``CARGO_HOME`` and ``CARGO_TARGET_DIR`` values for one project build."""
    raise NotImplementedError
