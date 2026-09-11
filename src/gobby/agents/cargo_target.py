"""One shared cargo build directory per project.

Every checkout of a Cargo project (the primary checkout, every task worktree,
every clone) builds into ``~/.gobby/cache/cargo-target/<project_id>/`` so the
per-checkout million-file ``target/`` trees disappear and incremental builds
reuse each other. Interactive shells reach the shared directory through a
``<checkout>/target`` symlink that Gobby creates when it registers a checkout;
spawned agents receive ``CARGO_TARGET_DIR`` in their environment, which cargo
honors ahead of the symlink. Cargo's build-directory lock serializes concurrent
builds across checkouts ("Blocking waiting for file lock on build directory").
A pre-existing real ``target/`` directory is never touched: the operator moves
it aside to join the share.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def shared_cargo_target_dir(project_id: str) -> Path:
    """Return the project's shared cargo target directory under Gobby home."""
    safe_id = _UNSAFE_CHARS.sub("-", project_id).strip("-")[:80] or "unknown-project"
    return get_gobby_home() / "cache" / "cargo-target" / safe_id


def ensure_shared_cargo_target_dir(project_id: str) -> str:
    """Create the project's shared cargo target directory and return its path."""
    target_dir = shared_cargo_target_dir(project_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    return str(target_dir)


def link_checkout_cargo_target(checkout: Path, project_id: str) -> bool:
    """Point ``<checkout>/target`` at the project's shared cargo target directory.

    Returns True when the link exists afterwards. A checkout without a
    ``Cargo.toml`` is left alone, as is any existing ``target`` entry that is
    not already the shared link; nothing is ever deleted and nothing raises.
    """
    if not (checkout / "Cargo.toml").is_file():
        return False
    target = checkout / "target"
    shared = shared_cargo_target_dir(project_id)
    try:
        if target.is_symlink():
            if os.readlink(target) == str(shared):
                return True
            logger.debug("Leaving existing target at %s; move it aside to share builds", target)
            return False
        if target.exists():
            logger.debug("Leaving existing target at %s; move it aside to share builds", target)
            return False
        shared.mkdir(parents=True, exist_ok=True)
        os.symlink(shared, target, target_is_directory=True)
    except OSError:
        logger.warning("Failed to link %s to %s", target, shared, exc_info=True)
        return False
    return True
