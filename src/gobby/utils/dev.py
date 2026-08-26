"""Dev mode and source-tree detection utilities."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WORKTREE_DAEMON_OVERRIDE_ENV",
    "LinkedWorktree",
    "has_gobby_pyproject",
    "is_dev_mode",
    "is_gobby_project",
    "linked_worktree_root",
    "running_source_worktree",
    "worktree_daemon_refusal",
]

WORKTREE_DAEMON_OVERRIDE_ENV = "GOBBY_ALLOW_WORKTREE_DAEMON"


def has_gobby_pyproject(path: Path) -> bool:
    """Check if a directory has a pyproject.toml for the gobby project.

    Weaker check than is_gobby_project — only requires pyproject.toml,
    not the full source tree. Used by the service installer which needs
    to detect dev mode before the source tree is fully built.

    Args:
        path: Directory to check

    Returns:
        True if pyproject.toml with name="gobby" exists
    """
    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        content = pyproject.read_text(encoding="utf-8")
        return 'name = "gobby"' in content or "name = 'gobby'" in content
    except OSError:
        return False


def is_gobby_project(path: Path) -> bool:
    """Check if a directory is the gobby source repository.

    Looks for the canonical marker: src/gobby/install/shared/ directory
    AND a pyproject.toml with name = "gobby".

    Args:
        path: Directory to check

    Returns:
        True if the path is the gobby source repo root
    """
    if not (path / "src" / "gobby" / "install" / "shared").is_dir():
        return False
    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        content = pyproject.read_text(encoding="utf-8")
        return 'name = "gobby"' in content or "name = 'gobby'" in content
    except OSError:
        return False


def is_dev_mode(project_path: Path | None = None) -> bool:
    """Detect if running inside the gobby source repo.

    When the project IS the gobby source repo, bundled resources are editable
    directly (no copies needed). This is used to gate write access to
    scope='bundled' records in the database.

    Args:
        project_path: Path to check (defaults to cwd)

    Returns:
        True if the path is inside the gobby source repo
    """
    path = project_path or Path.cwd()
    return any(is_gobby_project(candidate) for candidate in (path, *path.parents))


@dataclass(frozen=True)
class LinkedWorktree:
    """A git linked worktree and the main checkout that owns its ``.git``."""

    root: Path
    main_checkout: Path


def linked_worktree_root(path: Path) -> LinkedWorktree | None:
    """Return the linked git worktree containing ``path``, or None.

    A linked worktree's root holds a ``.git`` *file* whose ``gitdir:`` line
    points at ``<main>/.git/worktrees/<name>``. The main working tree holds a
    ``.git`` directory, a submodule's ``.git`` file points under
    ``.git/modules/``, and a non-git install has neither; all of those return
    None. The nearest ``.git`` entry decides.
    """
    for candidate in (path, *path.parents):
        git_entry = candidate / ".git"
        if git_entry.is_dir():
            return None
        if not git_entry.is_file():
            continue
        try:
            content = git_entry.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not content.startswith("gitdir:"):
            return None
        git_dir = Path(content.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = (candidate / git_dir).resolve()
        if git_dir.parent.name != "worktrees" or git_dir.parent.parent.name != ".git":
            return None
        return LinkedWorktree(root=candidate, main_checkout=git_dir.parent.parent.parent)
    return None


def running_source_worktree() -> LinkedWorktree | None:
    """Return the linked worktree the imported ``gobby`` package lives in, if any."""
    import gobby

    module_file = getattr(gobby, "__file__", None)
    if not module_file:
        return None
    return linked_worktree_root(Path(module_file).resolve().parent)


def worktree_daemon_refusal(environ: Mapping[str, str] | None = None) -> str | None:
    """Explain why the daemon must not start from the running source tree, or None.

    Daemon startup syncs the bundled templates shipped with the running code
    into the shared database, so a daemon started from a linked worktree
    rewrites rules, skills, and workflows for every session from a branch that
    may be stale or unmerged (#21031). ``GOBBY_ALLOW_WORKTREE_DAEMON=1``
    overrides the refusal for deliberate, announced testing.
    """
    env = os.environ if environ is None else environ
    if env.get(WORKTREE_DAEMON_OVERRIDE_ENV) == "1":
        return None
    worktree = running_source_worktree()
    if worktree is None:
        return None
    return (
        f"Refusing to start the Gobby daemon from linked worktree {worktree.root}: "
        "startup syncs this checkout's bundled templates into the shared database. "
        f"Start it from the main checkout {worktree.main_checkout} instead, or set "
        f"{WORKTREE_DAEMON_OVERRIDE_ENV}=1 to override deliberately."
    )
