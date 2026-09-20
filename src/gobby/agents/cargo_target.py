"""Checkout-specific Cargo build directories.

Each checkout builds into a deterministic directory below
``~/.gobby/cache/cargo-target-v2/<project_id>/``. The checkout path participates
in the cache key, so a main checkout, worktree, and clone never exchange build
artifacts. Interactive shells reach the directory through a ``target`` symlink;
spawned agents receive the same path as ``CARGO_TARGET_DIR``. The shared
``CARGO_HOME`` remains separate and continues to reuse registry and Git inputs.

The link is added to the repository's local exclude file because the conventional
``target/`` ignore pattern matches directories and leaves a symlink untracked.
Existing real directories and foreign symlinks are preserved. Gobby's legacy
project-wide symlink is replaced atomically without copying its artifacts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# `.gitignore` conventionally carries `target/`, which matches directories only,
# so the symlink Gobby creates would show up as untracked in every checkout.
# The pattern is anchored at the working-tree root and lives in the repository's
# local exclude file, which every linked worktree of that repository shares.
_TARGET_EXCLUDE_PATTERN = "/target"
_PATH_HASH_LENGTH = 16
_PROJECT_COMPONENT_LENGTH = 80
_CHECKOUT_COMPONENT_LENGTH = 64


def _safe_component(value: str, *, limit: int, fallback: str) -> str:
    return _UNSAFE_CHARS.sub("-", value).strip("-")[:limit] or fallback


def _project_cache_root(project_id: str) -> Path:
    safe_id = _safe_component(
        project_id,
        limit=_PROJECT_COMPONENT_LENGTH,
        fallback="unknown-project",
    )
    return get_gobby_home() / "cache" / "cargo-target-v2" / safe_id


def _legacy_cargo_target_dir(project_id: str) -> Path:
    safe_id = _safe_component(
        project_id,
        limit=_PROJECT_COMPONENT_LENGTH,
        fallback="unknown-project",
    )
    return get_gobby_home() / "cache" / "cargo-target" / safe_id


def checkout_cargo_target_dir(checkout: Path, project_id: str) -> Path:
    """Return the deterministic Cargo target directory for one checkout."""
    canonical = checkout.expanduser().resolve(strict=False)
    safe_name = _safe_component(
        canonical.name,
        limit=_CHECKOUT_COMPONENT_LENGTH,
        fallback="checkout",
    )
    path_hash = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:_PATH_HASH_LENGTH]
    return _project_cache_root(project_id) / f"{safe_name}-{path_hash}"


def ensure_checkout_cargo_target_dir(checkout: Path, project_id: str) -> str:
    """Create one checkout's Cargo target directory and return its path."""
    target_dir = checkout_cargo_target_dir(checkout, project_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    return str(target_dir)


def cleanup_checkout_cargo_target_dir(checkout: Path, project_id: str) -> str | None:
    """Remove one Gobby-owned checkout target, returning an error message on failure."""
    project_root = _project_cache_root(project_id)
    target_dir = checkout_cargo_target_dir(checkout, project_id)
    if target_dir.parent != project_root or target_dir == project_root:
        return f"Refusing to remove unguarded Cargo target path: {target_dir}"
    if project_root.is_symlink():
        return f"Refusing to remove Cargo target below symlinked project cache: {project_root}"
    try:
        if target_dir.is_symlink():
            return f"Refusing to remove symlinked Cargo target path: {target_dir}"
        shutil.rmtree(target_dir)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Failed to remove Cargo target %s", target_dir, exc_info=True)
        return str(exc)
    return None


def _replace_target_symlink(target: Path, destination: Path) -> None:
    temporary = target.with_name(f".{target.name}.gobby-{uuid.uuid4().hex}")
    try:
        os.symlink(destination, temporary, target_is_directory=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def link_checkout_cargo_target(checkout: Path, project_id: str) -> bool:
    """Point ``<checkout>/target`` at its checkout-specific Cargo target.

    Returns True when the link exists afterwards. A checkout without a
    ``Cargo.toml`` is left alone, as is any existing ``target`` entry that is
    neither the desired link nor Gobby's legacy shared link. Nothing raises.
    """
    if not (checkout / "Cargo.toml").is_file():
        return False
    target = checkout / "target"
    desired = checkout_cargo_target_dir(checkout, project_id)
    legacy = _legacy_cargo_target_dir(project_id)
    try:
        if target.is_symlink():
            destination = os.readlink(target)
            if destination == str(desired):
                desired.mkdir(parents=True, exist_ok=True)
            elif destination == str(legacy):
                desired.mkdir(parents=True, exist_ok=True)
                _replace_target_symlink(target, desired)
            else:
                logger.debug("Leaving foreign target symlink at %s", target)
                return False
        elif target.exists():
            logger.debug("Leaving existing target directory at %s", target)
            return False
        else:
            desired.mkdir(parents=True, exist_ok=True)
            _replace_target_symlink(target, desired)
    except OSError:
        logger.warning("Failed to link %s to %s", target, desired, exc_info=True)
        return False
    exclude_checkout_target(checkout)
    return True


def exclude_checkout_target(checkout: Path) -> bool:
    """Add the checkout-target link to the repository's local exclude file.

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
        logger.warning("Failed to exclude the checkout target link in %s", checkout, exc_info=True)
        return False
    return True


def _git_common_dir(checkout: Path) -> Path | None:
    """Resolve the repository directory shared by a checkout and its worktrees.

    Reads the documented repository layout rather than spawning git: the daemon
    calls this on session activation and worktree setup, where a subprocess per
    call is unnecessary for this deterministic cache routing.
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
