"""Bounded tmux scrollback capture for terminal attach.

``capture-pane`` runs as its own subprocess here rather than through
:meth:`TmuxSessionManager._run`: that helper decodes strictly (a pane holding
invalid UTF-8 raises) and owns its own timeout, neither of which suits a
best-effort history read on the attach path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.agents.tmux.session_manager import TmuxSessionManager

logger = logging.getLogger(__name__)

# The governing bound. This is a renderer budget, not a wire budget: the client
# writes the whole window in one call, and the Ghostty VT core costs ~0.75 ms
# per line to ingest it. Measured under the pinned protocol (4x CPU throttle, 1
# warm-up, 5 samples, median, timed from frame send to a settled scrollback
# render): 300 lines -> 229 ms, 400 -> 338 ms, 500 -> 470 ms, 2000 -> 1690 ms.
# 300 is the largest bound clearing the 250 ms budget. The wterm fallback core
# is ~6x cheaper (57 ms at 300) and never binds. Ghostty's default ring also
# retains only ~823 rows, so a larger window would be evicted on arrival.
ATTACH_HISTORY_LINES = 300
# Backstop only, sized so the line count decides and bytes catch just the
# pathological per-cell-color case.
ATTACH_HISTORY_MAX_BYTES = 1024 * 1024
CAPTURE_TIMEOUT_SECONDS = 5.0

_REAP_TIMEOUT_SECONDS = 2.0
_SGR_RESET = "\x1b[0m"


@dataclass(frozen=True)
class HistoryCapture:
    """A bounded scrollback window, ready to write to a VT.

    ``dropped_bytes`` and ``total_bytes`` describe the *delivered window* --
    the buffer older than the requested line range was never read, so counting
    it would be fabrication. Line truncation is reported by ``truncated``.
    """

    text: str
    truncated: bool
    dropped_bytes: int
    total_bytes: int


class HistoryCaptureError(RuntimeError):
    """``capture-pane`` timed out or exited nonzero."""


def build_capture_args(
    manager: TmuxSessionManager,
    session_name: str,
    *,
    max_lines: int = ATTACH_HISTORY_LINES,
) -> list[str]:
    """Build the ``capture-pane`` argv for a bounded history read.

    ``-e`` preserves SGR. ``-J`` is deliberately absent: it joins soft-wrapped
    lines, the opposite of preserving them. ``-E -1`` ends one line above the
    visible pane so history and the repaint that follows do not overlap.
    ``max_lines + 1`` is the truncation probe -- asking for one line more than
    the bound is the only way to observe that older history existed.
    """
    from gobby.agents.tmux.session_manager import _exact_session_target

    return [
        *manager.base_args(),
        "capture-pane",
        "-t",
        _exact_session_target(session_name),
        "-p",
        "-e",
        f"-S-{max_lines + 1}",
        "-E",
        "-1",
    ]


def bound_history(
    raw: str,
    *,
    max_lines: int = ATTACH_HISTORY_LINES,
    max_bytes: int = ATTACH_HISTORY_MAX_BYTES,
) -> HistoryCapture:
    """Window a raw capture to the line and byte bounds.

    All accounting happens in one LF-normalized UTF-8 domain. The ``\\r\\n``
    join and the trailing SGR reset are applied last, so neither the CRLF
    expansion nor the reset can perturb the counters.
    """
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    lines = normalized.split("\n") if normalized else []

    line_truncated = len(lines) > max_lines
    if line_truncated:
        lines = lines[len(lines) - max_lines :]

    windowed = "\n".join(lines)
    encoded = windowed.encode("utf-8")
    total_bytes = len(encoded)

    if max_bytes <= 0:
        retained = ""
    elif total_bytes > max_bytes:
        cut_index = total_bytes - max_bytes
        # errors="ignore" repairs the leading partial codepoint at the cut.
        retained = encoded[cut_index:].decode("utf-8", errors="ignore")
        # A cut landing exactly on a line boundary yields a complete first
        # line; only a mid-line cut leaves a partial one to drop.
        if encoded[cut_index - 1 : cut_index] != b"\n":
            boundary = retained.find("\n")
            retained = retained[boundary + 1 :] if boundary != -1 else ""
    else:
        retained = windowed

    dropped_bytes = total_bytes - len(retained.encode("utf-8"))

    # capture-pane emits bare LF, and a VT treats LF as line-feed-only with no
    # carriage return -- writing the capture verbatim staircases.
    text = "\r\n".join(retained.split("\n")) + _SGR_RESET if retained else ""

    return HistoryCapture(
        text=text,
        truncated=line_truncated or dropped_bytes > 0,
        dropped_bytes=dropped_bytes,
        total_bytes=total_bytes,
    )


async def capture_history(
    manager: TmuxSessionManager,
    session_name: str,
    *,
    max_lines: int = ATTACH_HISTORY_LINES,
    max_bytes: int = ATTACH_HISTORY_MAX_BYTES,
    timeout: float = CAPTURE_TIMEOUT_SECONDS,
) -> HistoryCapture:
    """Capture a bounded scrollback window for ``session_name``.

    Raises:
        HistoryCaptureError: capture-pane timed out or exited nonzero.
    """
    args = build_capture_args(manager, session_name, max_lines=max_lines)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await kill_and_reap(proc)
        raise HistoryCaptureError(
            f"tmux capture-pane timed out after {timeout}s for '{session_name}'"
        ) from None
    except asyncio.CancelledError:
        await kill_and_reap(proc)
        raise

    if proc.returncode:
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        raise HistoryCaptureError(
            f"tmux capture-pane failed for '{session_name}' "
            f"(rc={proc.returncode}): {stderr or 'no stderr'}"
        )

    raw = (stdout_bytes or b"").decode("utf-8", errors="replace")
    return bound_history(raw, max_lines=max_lines, max_bytes=max_bytes)


async def kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill a short-lived tmux read child and wait for it, leaving no orphan."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        # Shielded so an already-pending cancellation still reaps the child in
        # the background instead of abandoning it.
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_REAP_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.CancelledError):
        logger.debug("capture-pane child %s did not reap promptly", proc.pid)
