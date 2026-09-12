"""One shared cargo build directory per project.

Every checkout of a Cargo project (the primary checkout, every task worktree,
every clone) builds into ``~/.gobby/cache/cargo-target/<project_id>/`` so the
per-checkout million-file ``target/`` trees disappear and incremental builds
reuse each other. Interactive shells reach the shared directory through a
``<checkout>/target`` symlink that Gobby creates when it registers a checkout;
spawned agents receive ``CARGO_TARGET_DIR`` in their environment, which cargo
honors ahead of the symlink. Cargo's build-directory lock serializes concurrent
builds across checkouts ("Blocking waiting for file lock on build directory").
The link itself is added to the repository's local exclude file, because the
conventional ``target/`` ignore pattern matches directories and would leave the
symlink showing as untracked in every checkout. A pre-existing real ``target/``
directory is never touched: the operator moves it aside to join the share.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# `.gitignore` conventionally carries `target/`, which matches directories only,
# so the symlink Gobby creates would show up as untracked in every checkout.
# The pattern is anchored at the working-tree root and lives in the repository's
# local exclude file, which every linked worktree of that repository shares.
_TARGET_EXCLUDE_PATTERN = "/target"


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
            if os.readlink(target) != str(shared):
                logger.debug("Leaving existing target at %s; move it aside to share builds", target)
                return False
        elif target.exists():
            logger.debug("Leaving existing target at %s; move it aside to share builds", target)
            return False
        else:
            shared.mkdir(parents=True, exist_ok=True)
            os.symlink(shared, target, target_is_directory=True)
    except OSError:
        logger.warning("Failed to link %s to %s", target, shared, exc_info=True)
        return False
    exclude_checkout_target(checkout)
    return True


def exclude_checkout_target(checkout: Path) -> bool:
    """Add the shared-target link to the repository's local exclude file.

    Returns True when the pattern is present afterwards. A checkout that is not
    a work tree is left alone, and no failure propagates.
    """
    common_dir = _git_common_dir(checkout)
    if common_dir is None or not common_dir.is_dir():
        return False
    exclude_path = common_dir / "info" / "exclude"
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
        if _TARGET_EXCLUDE_PATTERN in {line.strip() for line in existing.splitlines()}:
            return True
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude_path.write_text(
            f"{existing}{separator}{_TARGET_EXCLUDE_PATTERN}\n", encoding="utf-8"
        )
    except OSError:
        logger.warning("Failed to exclude the shared target link in %s", checkout, exc_info=True)
        return False
    return True


def _git_common_dir(checkout: Path) -> Path | None:
    """Resolve the repository directory shared by a checkout and its worktrees.

    Reads the documented repository layout rather than spawning git: the daemon
    calls this on session activation and worktree setup, where a subprocess per
    call is exactly the cost this shared build directory exists to remove.
    """
    git_path = checkout / ".git"
    if git_path.is_dir():
        return git_path
    if not git_path.is_file():
        return None
    try:
        pointer = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir:"):
        return None
    git_dir = _resolve_against(checkout, pointer.removeprefix("gitdir:").strip())
    if git_dir is None:
        return None
    common_pointer = git_dir / "commondir"
    if not common_pointer.is_file():
        return git_dir
    try:
        return _resolve_against(git_dir, common_pointer.read_text(encoding="utf-8").strip())
    except OSError:
        return git_dir


def _resolve_against(base: Path, raw_path: str) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return (base / candidate).resolve()
