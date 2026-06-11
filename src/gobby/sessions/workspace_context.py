"""Workspace and git context helpers for session summaries."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from gobby.sessions.tmux_context import parse_terminal_context_value

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


def resolve_session_workspace(session: Any, transcript_path: str | None = None) -> Path:
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


async def enrich_git_context(handoff_ctx: Any, cwd: Path) -> None:
    """Enrich HandoffContext with real-time git status and commits."""
    if not handoff_ctx.git_status:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            handoff_ctx.git_status = stdout.decode().strip() if proc.returncode == 0 else ""
        except Exception as e:
            logger.debug("Failed to get git status for %s: %s", cwd, e)

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "--oneline",
            "-10",
            "--format=%H|%s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            commits = []
            for line in stdout.decode().strip().split("\n"):
                if "|" in line:
                    hash_val, message = line.split("|", 1)
                    commits.append({"hash": hash_val, "message": message})
            if commits:
                handoff_ctx.git_commits = commits
    except Exception as e:
        logger.debug("Failed to get git log for %s: %s", cwd, e)
