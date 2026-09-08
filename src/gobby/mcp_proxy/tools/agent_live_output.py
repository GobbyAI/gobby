"""Bounded live terminal output for agent query tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from gobby.storage.agents import TERMINAL_AGENT_RUN_STATUSES
from gobby.terminals.runtime import SnapshotResult

logger = logging.getLogger(__name__)

LIVE_OUTPUT_MAX_LINES = 200
LIVE_OUTPUT_MAX_CHARS = 4_096
_LIVE_OUTPUT_SOURCE = "terminal_snapshot"
_LIVE_OUTPUT_ORDERING = "oldest_to_newest"


class TerminalSnapshotReader(Protocol):
    """Backend-neutral terminal snapshot surface used by agent reads."""

    async def snapshot(self, run: Any, lines: int = 50) -> SnapshotResult | None: ...


def live_output_reference(run: Any) -> dict[str, Any]:
    """Describe the opt-in live-output path without capturing terminal content."""
    reference: dict[str, Any] = {
        "available": True,
        "source": _LIVE_OUTPUT_SOURCE,
        "ordering": _LIVE_OUTPUT_ORDERING,
        "line_limit": LIVE_OUTPUT_MAX_LINES,
        "char_limit": LIVE_OUTPUT_MAX_CHARS,
        "retrieval_tool": "get_agent_live_output",
    }
    if run.status in TERMINAL_AGENT_RUN_STATUSES:
        reference.update(available=False, reason="run_terminal")
    elif not getattr(run, "terminal_id", None):
        reason = "terminal_not_ready" if run.status == "pending" else "terminal_unavailable"
        reference.update(available=False, reason=reason)
    return reference


async def read_live_output(
    run: Any,
    terminal_services: TerminalSnapshotReader | None,
) -> dict[str, Any]:
    """Capture and bound the newest active terminal output in source order."""
    reference = live_output_reference(run)
    if not reference["available"]:
        return reference
    if terminal_services is None:
        return {**reference, "available": False, "reason": "terminal_services_unavailable"}

    try:
        snapshot = await terminal_services.snapshot(run, lines=LIVE_OUTPUT_MAX_LINES + 1)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Failed to capture live output for agent run %s",
            getattr(run, "id", None),
            exc_info=True,
        )
        return {**reference, "available": False, "reason": "capture_failed"}
    if snapshot is None:
        return {**reference, "available": False, "reason": "terminal_unavailable"}

    source_lines = snapshot.text.splitlines(keepends=True)
    line_truncated = len(source_lines) > LIVE_OUTPUT_MAX_LINES
    line_bounded = "".join(source_lines[-LIVE_OUTPUT_MAX_LINES:])
    char_truncated = len(line_bounded) > LIVE_OUTPUT_MAX_CHARS
    content = line_bounded[-LIVE_OUTPUT_MAX_CHARS:]
    backend_truncated = snapshot.truncated
    return {
        **reference,
        "content": content,
        "char_count": len(content),
        "line_count": len(content.splitlines()),
        "truncated": line_truncated or char_truncated or backend_truncated,
        "truncation": {
            "line_limit": line_truncated,
            "char_limit": char_truncated,
            "backend": backend_truncated,
        },
    }
