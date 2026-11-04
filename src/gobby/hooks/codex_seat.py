"""Ownership and origin checks for adopting a Codex seat TUI for a new thread."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from gobby.hooks.terminal_context import SeatAvailable, SeatOwner, SeatOwnerCheck
from gobby.storage.hub.protocol import HubDatabase

# Same tolerance as terminal_ownership.recorded_process_is_alive.
_CREATE_TIME_TOLERANCE_SECONDS = 1.0
# The rollout originator Codex writes for a thread its terminal TUI minted.
CODEX_TUI_ORIGINATOR = "codex-tui"
# The session_meta header carries the base instructions, so it can run long.
_MAX_ROLLOUT_HEADER_BYTES = 1 << 20


def codex_thread_originator(transcript_path: str | None, thread_id: str) -> str | None:
    """Return the client that minted ``thread_id``, from its rollout's session_meta header.

    Returns None unless the file is a readable regular rollout whose header names this
    thread, so callers treat an unknown origin the same as an unverified one.
    """
    if not transcript_path:
        return None
    try:
        # Non-blocking, so a pipe at this path fails the regular-file check instead of
        # hanging the hook until something writes to it.
        fd = os.open(Path(transcript_path).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            header = json.loads(stream.readline(_MAX_ROLLOUT_HEADER_BYTES))
    except (OSError, ValueError):
        return None
    if not isinstance(header, dict) or header.get("type") != "session_meta":
        return None
    payload = header.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("id") != thread_id:
        return None
    originator = payload.get("originator")
    return originator if isinstance(originator, str) and originator else None


def codex_seat_available(db: HubDatabase, machine_id: str) -> SeatAvailable:
    """Return a check that no Codex terminal session owns, or may own, a seat TUI."""
    owner_of = codex_seat_owner(db, machine_id)

    def available(pid: int, create_time: float) -> bool:
        return owner_of(pid, create_time) is SeatOwner.NONE

    return available


def codex_seat_owner(db: HubDatabase, machine_id: str) -> SeatOwnerCheck:
    """Return a check that classifies who owns a seat TUI.

    A session of any status that recorded the TUI's pid and create time owns it. A
    terminal session with no recorded process, created after the TUI started, may be an
    earlier thread of that same TUI, so it makes the seat a possible owner's too.
    """

    def owner_of(pid: int, create_time: float) -> SeatOwner:
        rows = db.fetchall(
            """
            SELECT terminal_context->>'parent_pid' AS parent_pid,
                   terminal_context->>'parent_create_time' AS parent_create_time
            FROM sessions
            WHERE source = 'codex' AND machine_id = %s AND session_type = 'terminal'
              AND (
                terminal_context->>'parent_pid' = %s
                OR (
                  NULLIF(terminal_context->>'parent_pid', '') IS NULL
                  AND created_at > %s
                )
              )
            """,
            (machine_id, str(pid), datetime.fromtimestamp(create_time, UTC)),
        )
        owner = SeatOwner.NONE
        for row in rows:
            if not row["parent_pid"]:
                owner = SeatOwner.POSSIBLE
                continue
            try:
                recorded = float(row["parent_create_time"])
            except (TypeError, ValueError):
                owner = SeatOwner.POSSIBLE
                continue
            if abs(recorded - create_time) < _CREATE_TIME_TOLERANCE_SECONDS:
                return SeatOwner.RECORDED
        return owner

    return owner_of
