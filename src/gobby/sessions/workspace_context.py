"""Workspace and git context helpers for session summaries."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.sessions.tmux_context import parse_terminal_context_value
from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git, parse_porcelain_v1_z

if TYPE_CHECKING:
    from gobby.sessions.analyzer import HandoffContext
    from gobby.storage.session_models import Session

logger = logging.getLogger(__name__)


def _coerce_path(value: Any) -> Path | None:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, os.PathLike):
        text = os.fspath(value).strip()
    else:
        return None

    if not text:
        return None
    return Path(text).expanduser()


def resolve_session_workspace(session: Session, transcript_path: str | None = None) -> Path:
    """Resolve the workspace cwd for summary git context."""
    terminal_context = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if terminal_context:
        cwd = _coerce_path(terminal_context.get("cwd"))
        if cwd is not None:
            return cwd

    if transcript_path:
        path = Path(transcript_path)
        if path.exists():
            return path.parent

    return Path.cwd()


async def _missing_workspace_git_context(cwd: Path) -> str | None:
    """Report why git context is unavailable for a workspace, or None if it is usable.

    Existing on disk is not enough. A workspace that survives as a plain directory
    after its worktree is removed still fails every git query, and `git diff HEAD`
    answers that by falling back to `--no-index` and printing a usage dump rather
    than a clean error. Callers degrade on the message returned here, so proving
    the workspace is still inside a worktree is what keeps that dump out of them.
    """
    try:
        cwd.stat()
    except FileNotFoundError:
        return f"[git context unavailable: session workspace no longer exists: {cwd}]"
    except OSError:
        return None

    probe = await daemon_git.run(["rev-parse", "--git-dir"], cwd=cwd, timeout=5.0)
    if isinstance(probe, GitFailed) and "not a git repository" in probe.stderr.lower():
        return f"[git context unavailable: session workspace is not a git repository: {cwd}]"
    # Any other failure (timeout, unreadable config, dubious ownership) is not
    # evidence that the repository is gone, so behavior stays as it was.
    return None


async def enrich_git_context(handoff_ctx: HandoffContext, cwd: Path) -> None:
    """Enrich HandoffContext with real-time git status and commits."""
    if not handoff_ctx.files_modified:
        return

    missing_git_context = await _missing_workspace_git_context(cwd)
    if missing_git_context:
        handoff_ctx.git_status = missing_git_context
        return

    paths = _session_git_paths(handoff_ctx.files_modified, cwd)
    handoff_ctx.git_status = ""
    handoff_ctx.git_commits = []
    if not paths:
        return

    status_result, log_result = await asyncio.gather(
        daemon_git.status(cwd, paths, timeout=5.0),
        daemon_git.run(
            [
                "--literal-pathspecs",
                "log",
                "--oneline",
                "-10",
                "--format=%H|%s",
                "--",
                *paths,
            ],
            cwd=cwd,
            timeout=5.0,
        ),
    )

    if isinstance(status_result, GitOk):
        handoff_ctx.git_status = "\n".join(
            f"{entry.code} {entry.path}" for entry in parse_porcelain_v1_z(status_result.stdout)
        )
    else:
        detail = status_result.stderr.strip() or status_result.status
        handoff_ctx.git_status = f"[git status unavailable: {detail}]"
        logger.debug("Failed to get git status for %s: %s", cwd, detail)

    if isinstance(log_result, GitOk):
        commits = []
        for line in log_result.stdout.strip().split("\n"):
            if "|" in line:
                hash_val, message = line.split("|", 1)
                commits.append({"hash": hash_val, "message": message})
        if commits:
            handoff_ctx.git_commits = commits
    else:
        logger.debug(
            "Failed to get git log for %s: %s",
            cwd,
            log_result.stderr.strip() or log_result.status,
        )


def _session_git_paths(files_modified: list[str], cwd: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for raw_path in files_modified:
        path = Path(raw_path)
        if path.is_absolute():
            try:
                path = path.relative_to(cwd)
            except ValueError:
                continue
        value = path.as_posix()
        if value not in paths:
            paths.append(value)
    return tuple(paths)
