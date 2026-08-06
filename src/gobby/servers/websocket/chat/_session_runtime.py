"""Runtime identity and worktree discovery for web-chat sessions."""

import asyncio
import logging
from typing import Any

from gobby.servers.chat_session_base import ChatSessionProtocol

logger = logging.getLogger(__name__)


def _get_runtime_external_id(session: ChatSessionProtocol) -> str | None:
    """Return the provider-native session/thread id discovered during start()."""
    sdk_session_id = getattr(session, "sdk_session_id", None)
    if isinstance(sdk_session_id, str) and sdk_session_id:
        return sdk_session_id

    thread_id = getattr(session, "_thread_id", None)
    if isinstance(thread_id, str) and thread_id:
        return thread_id

    return None


def _get_runtime_transcript_path(session: ChatSessionProtocol) -> str | None:
    """Return the live transcript path discovered during start(), if available."""
    transcript_path = getattr(session, "transcript_path", None)
    if isinstance(transcript_path, str) and transcript_path:
        return transcript_path

    private_path = getattr(session, "_transcript_path", None)
    if isinstance(private_path, str) and private_path:
        return private_path

    return None


def _is_bootstrap_external_id(external_id: str | None) -> bool:
    """Return True when external_id is still a temporary web-chat bootstrap value."""
    return bool(external_id and external_id.startswith("web-chat-bootstrap:"))


def _has_meaningful_web_chat_history(session: Any) -> bool:
    """Return True when a web-chat row already has meaningful runtime history."""
    return bool(
        getattr(session, "message_count", 0)
        or getattr(session, "turn_count", 0)
        or getattr(session, "usage_output_tokens", 0)
    )


async def _resolve_git_branch(project_path: str | None) -> tuple[str | None, str | None]:
    """Resolve the current git branch for a project directory.

    Returns (branch_name, worktree_path). branch_name is None for detached HEAD
    or non-git directories.
    """
    if not project_path:
        return None, None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--show-current",
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        branch = stdout.decode().strip() or None
        # For detached HEAD, show short SHA instead of nothing
        if not branch:
            proc2 = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--short",
                "HEAD",
                cwd=project_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5.0)
            short_sha = stdout2.decode().strip()
            if short_sha:
                branch = f"detached:{short_sha}"
        return branch, project_path
    except Exception as exc:
        logger.debug("Failed to resolve git branch: %s", exc)
        return None, None
