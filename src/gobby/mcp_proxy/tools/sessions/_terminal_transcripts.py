"""Transcript fallback helpers for terminal session tools."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

_TRANSCRIPT_TAIL_MAX_BYTES = 256 * 1024


def _read_transcript_tail_lines(path: Path, max_lines: int) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - _TRANSCRIPT_TAIL_MAX_BYTES)
        handle.seek(start)
        data = handle.read()

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and len(lines) > 1:
        lines = lines[1:]
    return lines[-max_lines:]


async def _capture_transcript_tail(
    session_id: str,
    session_manager: SessionManager,
    lines: int,
    *,
    tmux_error: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return transcript tail fallback when no live tmux target is available."""
    session = session_manager.get(session_id)
    if session is None:
        return None, "session_not_found"

    transcript_path = getattr(session, "transcript_path", None)
    if not isinstance(transcript_path, str) or not transcript_path:
        return None, "missing_transcript_path"

    path = Path(transcript_path)
    if not path.is_file():
        return None, "transcript_not_found"

    max_lines = max(1, lines)
    try:
        tail = await asyncio.to_thread(_read_transcript_tail_lines, path, max_lines)
    except OSError as exc:
        detail = str(exc) or type(exc).__name__
        return None, f"transcript_read_failed: {detail}"

    return (
        {
            "success": True,
            "output": "\n".join(tail),
            "via": "transcript",
            "transcript_path": transcript_path,
            "note": "No live tmux pane was capturable; returned transcript tail instead.",
            "tmux_error": tmux_error,
        },
        None,
    )
