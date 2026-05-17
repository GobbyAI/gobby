"""Integrity verification for bundled content.

Detects modifications to bundled YAML/MD files (workflows, skills, prompts,
rules, agents) by checking git status of the shared content directory or a
packaged raw-byte manifest when git is unavailable.

In dev mode (``is_dev_mode()``), integrity checks are skipped entirely —
file edits are expected. In production mode, any git-tracked modifications,
manifest hash mismatches, or untracked protected files are flagged as tampered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from gobby.install.manifest import (
    hash_file_bytes,
    iter_bundled_manifest_files,
    load_bundled_content_manifest,
)
from gobby.utils.git import run_git_command

logger = logging.getLogger(__name__)

IntegritySource = Literal["git", "manifest", "none"]

BUNDLED_SYNC_CONTENT_TYPES: set[str] = {
    "skills",
    "prompts",
    "agents",
    "pipelines",
    "rules",
    "variables",
    "build_profiles",
}

# Maps protected paths under install/shared/ to content type names used by
# sync_bundled_content_to_db's sync_targets.
CONTENT_TYPE_DIRS: dict[str, str] = {
    "skills": "skills",
    "prompts": "prompts",
    "workflows/rules": "rules",
    "rules": "rules",
    "workflows/agents": "agents",
    "workflows/variables": "variables",
    "workflows/pipelines": "pipelines",
    "registry/build_profiles": "build_profiles",
}

_GIT_PROTECTED_PATHS: tuple[str, ...] = (
    "skills",
    "prompts",
    "workflows",
    "rules",
    "registry/build_profiles.yaml",
)


@dataclass
class IntegrityResult:
    """Result of a bundled-content integrity check."""

    clean_files: list[str] = field(default_factory=list)
    dirty_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    git_available: bool = True
    checked: bool = False
    source: IntegritySource = "none"

    @property
    def all_clean(self) -> bool:
        """True when no dirty or untracked files were found."""
        return not self.dirty_files and not self.untracked_files


def verify_bundled_integrity(install_dir: Path) -> IntegrityResult:
    """Verify integrity of bundled content under *install_dir*/shared/.

    Git is preferred when available. Packaged installs without git fall back to
    the bundled raw-byte manifest.

    Git checks:
    - Modified tracked files (staged and unstaged) via ``git diff``
    - Untracked files via ``git ls-files --others``

    Manifest checks:
    - Missing manifest entries and hash mismatches as dirty files
    - Extra files under protected content roots as untracked files

    Args:
        install_dir: The ``src/gobby/install`` directory (parent of ``shared/``).

    Returns:
        An :class:`IntegrityResult` with lists of clean, dirty, and untracked files.
    """
    result = IntegrityResult()
    shared_dir = install_dir / "shared"

    if not shared_dir.is_dir():
        result.errors.append(f"Shared directory not found: {shared_dir}")
        result.git_available = False
        return result

    # Find the repo root so we can scope git commands
    repo_root = run_git_command(["git", "rev-parse", "--show-toplevel"], cwd=shared_dir)
    if repo_root is None:
        # Not a git repo — installed package context
        result.git_available = False
        return _verify_manifest_integrity(install_dir, result)

    repo_root_path = Path(repo_root)

    # Relative path of shared/ from repo root for scoping git commands
    try:
        rel_shared = shared_dir.resolve().relative_to(repo_root_path.resolve())
    except ValueError:
        result.errors.append(f"Shared dir {shared_dir} is not under repo root {repo_root_path}")
        result.git_available = False
        return _verify_manifest_integrity(install_dir, result)

    rel_shared_str = str(rel_shared)

    result.checked = True
    result.source = "git"

    content_dirs = [f"{rel_shared_str}/{path}" for path in _GIT_PROTECTED_PATHS]

    # 1. Unstaged modifications
    unstaged = run_git_command(
        ["git", "diff", "--name-only", "HEAD", "--"] + content_dirs,
        cwd=repo_root,
    )

    # 2. Staged modifications
    staged = run_git_command(
        ["git", "diff", "--cached", "--name-only", "--"] + content_dirs,
        cwd=repo_root,
    )

    # 3. Untracked files
    untracked = run_git_command(
        ["git", "ls-files", "--others", "--exclude-standard", "--"] + content_dirs,
        cwd=repo_root,
    )

    dirty: set[str] = set()
    if unstaged:
        dirty.update(f.strip() for f in unstaged.splitlines() if f.strip())
    if staged:
        dirty.update(f.strip() for f in staged.splitlines() if f.strip())

    result.dirty_files = sorted(dirty)

    if untracked:
        result.untracked_files = sorted(f.strip() for f in untracked.splitlines() if f.strip())

    # Build clean file list from tracked files minus dirty ones
    all_tracked = run_git_command(
        ["git", "ls-files", "--"] + content_dirs,
        cwd=repo_root,
    )
    if all_tracked:
        all_set = {f.strip() for f in all_tracked.splitlines() if f.strip()}
        result.clean_files = sorted(all_set - dirty)

    return result


def _verify_manifest_integrity(install_dir: Path, result: IntegrityResult) -> IntegrityResult:
    shared_dir = install_dir / "shared"
    manifest_files, errors = load_bundled_content_manifest(install_dir)
    if errors:
        result.errors.extend(errors)
    if manifest_files is None:
        result.source = "none"
        result.checked = False
        return result

    result.source = "manifest"
    result.checked = True

    clean: list[str] = []
    dirty: list[str] = []
    for relative_path, expected_hash in manifest_files.items():
        path = shared_dir / relative_path
        display_path = f"shared/{relative_path}"
        if not path.is_file():
            dirty.append(display_path)
            continue
        if hash_file_bytes(path) != expected_hash:
            dirty.append(display_path)
        else:
            clean.append(display_path)

    live_files = {
        path.relative_to(shared_dir).as_posix() for path in iter_bundled_manifest_files(shared_dir)
    }
    extra_files = sorted(live_files - set(manifest_files))

    result.clean_files = sorted(clean)
    result.dirty_files = sorted(dirty)
    result.untracked_files = [
        f"shared/{relative_path}"
        for relative_path in extra_files
        if _content_type_for_shared_relative_path(relative_path) is not None
    ]
    return result


def get_dirty_content_types(dirty_files: list[str], install_dir: Path) -> set[str]:
    """Map dirty file paths to content type names.

    Accepts git-relative paths such as
    ``src/gobby/install/shared/workflows/pipelines/foo.yaml`` and manifest
    paths such as ``shared/workflows/pipelines/foo.yaml``.

    Args:
        dirty_files: Dirty or untracked file paths.
        install_dir: The ``src/gobby/install`` directory.

    Returns:
        Set of sync target names (e.g. ``{"pipelines", "skills"}``).
    """
    affected: set[str] = set()
    for fpath in dirty_files:
        relative_path = _to_shared_relative_path(fpath, install_dir)
        if relative_path is None:
            continue
        content_type = _content_type_for_shared_relative_path(relative_path)
        if content_type is not None:
            affected.add(content_type)

    return affected


def _to_shared_relative_path(file_path: str, install_dir: Path) -> str | None:
    normalized = file_path.replace("\\", "/")
    if normalized.startswith("shared/"):
        return normalized[len("shared/") :]

    path = Path(file_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to((install_dir / "shared").resolve()).as_posix()
        except ValueError:
            return None

    marker = "/shared/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]

    return normalized


def _content_type_for_shared_relative_path(relative_path: str) -> str | None:
    parts = tuple(part for part in relative_path.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        return None

    if parts[0] in {"skills", "prompts", "rules"}:
        return CONTENT_TYPE_DIRS[parts[0]]

    if parts[0] == "workflows":
        if len(parts) == 2 and parts[1].endswith((".yaml", ".yml")):
            return "pipelines"
        if len(parts) >= 2:
            return CONTENT_TYPE_DIRS.get(f"workflows/{parts[1]}")
        return None

    if parts[0] == "registry" and len(parts) >= 2:
        if parts[1] == "build_profiles.yaml":
            return "build_profiles"

    return None
