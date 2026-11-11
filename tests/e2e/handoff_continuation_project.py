"""The handoff proof's fixed project folder under ~/.gobby (#25771).

The real seat runs with the operator's HOME and existing provider logins. The proof never
writes provider configuration. It runs in one fixed plain folder under ~/.gobby (Josh,
2026-11-09: "use a ~/.gobby/ folder"), so no seat works inside the live checkout or its git
repository. The folder is rebuilt fresh for every case and removed afterwards.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.e2e.composer_proof import ProofRefused
from tests.e2e.conftest import build_e2e_project, keep_e2e_artifacts


def project_folder(home: Path) -> Path:
    return home / ".gobby" / "e2e" / "handoff-continuation-project"


@pytest.fixture
def e2e_project_dir() -> Iterator[Path]:
    """The fixed project folder, rebuilt fresh for every case."""
    folder = project_folder(Path.home())
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ProofRefused(f"{folder} must be a real directory, not a symlink")
    folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    remove_project_folder(folder)
    folder.mkdir(mode=0o700)
    try:
        # A plain directory: the handoff cases never touch clones or worktrees.
        build_e2e_project(folder, git=False)
        yield folder
    finally:
        if not keep_e2e_artifacts():
            remove_project_folder(folder)


def _restore_write_and_retry(
    function: Callable[..., object], path: str, _exc: BaseException
) -> None:
    # The isolated daemon installs its sandbox runtime read-only under its HOME, which lives
    # inside this folder; restore the owner write bit on the parent (never following a link)
    # and retry, the same recovery TemporaryDirectory applies.
    parent = os.path.dirname(path)
    os.chmod(parent, stat.S_IRWXU, follow_symlinks=False)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, stat.S_IRWXU, follow_symlinks=False)
    function(path)


def remove_project_folder(folder: Path) -> None:
    """Remove the proof folder, read-only runtime trees included, and prove it is gone."""
    if not folder.exists() and not folder.is_symlink():
        return
    shutil.rmtree(folder, onexc=_restore_write_and_retry)
    if folder.exists() or folder.is_symlink():
        raise ProofRefused(f"{folder} could not be removed; clear it before the next proof")
