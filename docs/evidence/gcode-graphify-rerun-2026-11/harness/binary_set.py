"""Copy the installed gcode/gdaemon/ghook set into the isolated runtime as one set, by new inode."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from runtime_boundary import require


def install_binary_set(source_dir: Path, dest_dir: Path, set_files: list[str]) -> dict[str, Path]:
    """Copy every named member and the identity stamp, or copy nothing.

    The names come from the runtime's gobby (binary_set_files), so this module imports none.

    Each file is copied to a dotfile in the destination, then renamed over its name, so
    the installed file is a new inode and a stale copy never survives in place.
    """
    missing = [name for name in set_files if not (source_dir / name).is_file()]
    require(not missing, f"incomplete binary set in {source_dir}: missing {missing}")
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    installed: dict[str, Path] = {}
    for name in set_files:
        source = source_dir / name
        staging = dest_dir / f".{name}.staging"
        try:
            shutil.copyfile(source, staging)
            shutil.copymode(source, staging)
            os.replace(staging, dest_dir / name)
        finally:
            staging.unlink(missing_ok=True)
        installed[name] = dest_dir / name
    return installed
