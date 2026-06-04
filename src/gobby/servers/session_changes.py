"""Session-scoped working-tree change detection for the Changes panel.

The Changes activity panel must reflect the *viewed session's* working tree, not
the project repo. A session may run in the project repo, a git worktree, or a
clone; resumed sessions may have already committed their work. This module
resolves the session's working directory and diff base, then computes the
changed-file list and per-file diffs by running git there.

Resolution order:

1. Load the session and its project (the default repo).
2. If the session claimed a task with an isolated workspace (worktree/clone),
   prefer that path and diff against the recorded ``base_commit_sha`` so
   committed work still appears.
3. Otherwise diff the project repo against ``HEAD`` (uncommitted changes).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 10.0
# Internal paths that should never surface as user-facing changes.
_IGNORED_PATH_FRAGMENTS = (".gobby/", ".claude/plans/")


@dataclass(slots=True, frozen=True)
class SessionWorkspace:
    """The resolved working directory and diff base for a session."""

    working_dir: str
    base_ref: str
    isolation: str  # "none" | "worktree" | "clone"


@dataclass(slots=True, frozen=True)
class ChangedFile:
    """A single changed file. ``status`` is W (new), E (edited), or D (deleted)."""

    path: str
    status: str


async def _run_git(cwd: str, args: list[str], timeout: float = _GIT_TIMEOUT) -> tuple[int, str]:
    """Run a git command in ``cwd``, returning ``(returncode, stdout)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return 1, ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 1, ""
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


def _map_status(code: str) -> str:
    """Map a git --name-status code to the panel's W/E/D convention."""
    if code == "A":
        return "W"
    if code == "D":
        return "D"
    return "E"  # M, R, C, T, and anything else read as an edit.


def _is_ignored(path: str) -> bool:
    return any(fragment in path for fragment in _IGNORED_PATH_FRAGMENTS)


def is_safe_relative_path(working_dir: str, path: str) -> bool:
    """Return True if ``path`` resolves inside ``working_dir`` (no traversal)."""
    if not path or path.startswith("/"):
        return False
    try:
        base = Path(working_dir).resolve()
        target = (base / path).resolve()
    except (OSError, ValueError):
        return False
    return target == base or base in target.parents


def resolve_session_workspace(
    *,
    session_manager: Any,
    task_manager: Any,
    session_id: str,
) -> SessionWorkspace | None:
    """Resolve the working directory + diff base for a session.

    Returns None when the session is unknown or no readable working directory
    can be resolved.
    """
    session = session_manager.get(session_id)
    if session is None:
        return None

    repo_path: str | None = None
    try:
        from gobby.storage.projects import LocalProjectManager

        project = LocalProjectManager(session_manager.db).get(session.project_id)
        repo_path = project.repo_path if project else None
    except Exception:
        logger.debug("Failed to resolve project for session %s", session_id, exc_info=True)

    isolated = _resolve_isolated_workspace(task_manager, session_id)
    if isolated is not None:
        return isolated

    if repo_path and Path(repo_path).is_dir():
        return SessionWorkspace(working_dir=repo_path, base_ref="HEAD", isolation="none")
    return None


def _resolve_isolated_workspace(task_manager: Any, session_id: str) -> SessionWorkspace | None:
    """Return the isolated worktree/clone workspace claimed by this session, if any."""
    if task_manager is None:
        return None
    try:
        tasks = task_manager.list_tasks(claimed_by_session_id=session_id)
    except Exception:
        logger.debug("Failed to list tasks for session %s", session_id, exc_info=True)
        return None
    for task in tasks or []:
        try:
            artifacts = task_manager.artifacts.get_artifacts(task.id)
        except Exception:
            continue
        iso_path = artifacts.worktree_path or artifacts.clone_path
        if iso_path and Path(iso_path).is_dir():
            base = artifacts.base_commit_sha or "HEAD"
            kind = "worktree" if artifacts.worktree_path else "clone"
            return SessionWorkspace(working_dir=iso_path, base_ref=base, isolation=kind)
    return None


async def compute_session_changes(workspace: SessionWorkspace) -> list[ChangedFile]:
    """Compute the changed-file list for a workspace relative to its base ref."""
    cwd = workspace.working_dir
    base = workspace.base_ref
    files: dict[str, str] = {}

    rc, out = await _run_git(cwd, ["-c", "core.quotepath=false", "diff", base, "--name-status"])
    if rc == 0:
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code = parts[0][:1]
            # Renames/copies report "old\tnew"; the destination is the last column.
            path = parts[-1]
            files[path] = _map_status(code)

    rc_untracked, out_untracked = await _run_git(
        cwd, ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"]
    )
    if rc_untracked == 0:
        for line in out_untracked.splitlines():
            path = line.strip()
            if path and path not in files:
                files[path] = "W"

    result = [
        ChangedFile(path=path, status=status)
        for path, status in files.items()
        if not _is_ignored(path)
    ]
    # New files first, then edits, then deletes; alphabetical within each group.
    order = {"W": 0, "E": 1, "D": 2}
    result.sort(key=lambda f: (order.get(f.status, 3), f.path))
    return result


async def compute_session_file_diff(workspace: SessionWorkspace, path: str) -> str:
    """Compute the unified diff for a single file relative to the base ref."""
    cwd = workspace.working_dir
    base = workspace.base_ref

    rc, out = await _run_git(cwd, ["diff", base, "--", path])
    if rc == 0 and out.strip():
        return out

    # Untracked/new files are absent from the base; show them as a full addition.
    _, untracked = await _run_git(cwd, ["diff", "--no-index", "--", "/dev/null", path])
    return untracked if untracked.strip() else out
