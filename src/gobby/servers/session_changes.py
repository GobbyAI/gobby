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
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from gobby.storage.tasks import TaskArtifactConstraintError
from gobby.utils.git import run_git_command

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 10
_MAX_NEW_FILE_DIFF_BYTES = 1_000_000
_RECOVERABLE_WORKSPACE_ERRORS = (
    AttributeError,
    KeyError,
    TypeError,
    ValueError,
    psycopg.Error,
)
_RECOVERABLE_ARTIFACT_ERRORS = (
    *_RECOVERABLE_WORKSPACE_ERRORS,
    TaskArtifactConstraintError,
)
# Internal paths that should never surface as user-facing changes.
_IGNORED_PATH_FRAGMENTS = frozenset((".gobby/", ".claude/plans/"))


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


async def _git(cwd: str, args: list[str], timeout: int = _GIT_TIMEOUT) -> str | None:
    """Run a git command in ``cwd`` off the event loop.

    Reuses the shared ``gobby.utils.git.run_git_command`` helper (which returns
    stripped stdout on success, ``None`` on any non-zero exit) and runs it in a
    worker thread so the async route is not blocked.
    """
    return await asyncio.to_thread(run_git_command, ["git", *args], cwd, timeout)


def _new_file_diff(abs_path: Path, rel_path: str) -> str:
    """Synthesize a unified "new file" diff for an untracked file.

    ``run_git_command`` returns ``None`` for ``git diff --no-index`` (it exits 1
    when files differ), so build the added-file diff directly from contents.
    """
    try:
        file_size = os.path.getsize(abs_path)
        if file_size > _MAX_NEW_FILE_DIFF_BYTES:
            return (
                f"diff --git a/{rel_path} b/{rel_path}\n"
                "new file mode 100644\n"
                f"File too large to display ({file_size} bytes)\n"
            )
        with abs_path.open("rb") as file:
            raw_content = file.read(_MAX_NEW_FILE_DIFF_BYTES + 1)
        if len(raw_content) > _MAX_NEW_FILE_DIFF_BYTES:
            return (
                f"diff --git a/{rel_path} b/{rel_path}\n"
                "new file mode 100644\n"
                f"File too large to display (more than {_MAX_NEW_FILE_DIFF_BYTES} bytes)\n"
            )
        content = raw_content.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines = content.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{rel_path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


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
        from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
        from gobby.storage.workspace_machine_scope import require_local_machine_id

        if session.project_id:
            machine_id = require_local_machine_id(
                getattr(session, "machine_id", None),
                resource_kind="project_checkout",
                resource_id=session.project_id,
            )
            repo_path = require_root(session_manager.db, session.project_id, machine_id)
    except CheckoutNotFoundError:
        raise
    except _RECOVERABLE_WORKSPACE_ERRORS:
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
    except _RECOVERABLE_WORKSPACE_ERRORS:
        logger.debug("Failed to list tasks for session %s", session_id, exc_info=True)
        return None
    for task in tasks or []:
        try:
            artifacts = task_manager.artifacts.get_artifacts(task.id)
        except _RECOVERABLE_ARTIFACT_ERRORS:
            continue
        iso_path = artifacts.worktree_path or artifacts.clone_path
        if iso_path and Path(iso_path).is_dir():
            base = artifacts.base_commit_sha or "HEAD"
            kind = "worktree" if artifacts.worktree_path else "clone"
            return SessionWorkspace(working_dir=iso_path, base_ref=base, isolation=kind)
    return None


async def compute_session_changes(workspace: SessionWorkspace) -> list[ChangedFile]:
    """Compute the changed-file list for a workspace relative to its base ref.

    Runtime and OS failures from the underlying git helper propagate to callers
    so HTTP routes can classify them at the boundary.
    """
    cwd = workspace.working_dir
    base = workspace.base_ref
    files: dict[str, str] = {}

    out = await _git(cwd, ["-c", "core.quotepath=false", "diff", base, "--name-status"])
    if out:
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

    out_untracked = await _git(
        cwd, ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"]
    )
    if out_untracked:
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
    if not is_safe_relative_path(cwd, path):
        raise ValueError(
            f"unsafe or not a safe relative path: path={path!r} under working_dir={cwd!r}"
        )

    out = await _git(cwd, ["diff", base, "--", path])
    if out and out.strip():
        return out

    # Untracked/new files are absent from the base; show them as a full addition.
    return await asyncio.to_thread(_new_file_diff, Path(cwd) / path, path)
