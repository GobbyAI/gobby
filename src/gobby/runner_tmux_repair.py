"""Selection helpers for tmux window-name repair maintenance."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from gobby.terminal_ownership import (
    terminal_session_creation_order,
    terminal_session_identity,
)


class TmuxRepairSessionManager(Protocol):
    """Session-store operations required by tmux repair maintenance."""

    def list(self, *, statuses: list[str], limit: int) -> Sequence[Any]: ...


def _tmux_repair_pane_key(session: Any) -> tuple[str, str, str] | None:
    canonical_identity = terminal_session_identity(session)
    if canonical_identity is not None:
        return canonical_identity

    tc = getattr(session, "terminal_context", None)
    if not isinstance(tc, dict):
        return None

    pane = tc.get("tmux_pane")
    if not isinstance(pane, str) or not pane:
        return None

    socket = ""
    for key in ("tmux_socket_path", "tmux_socket_name", "tmux_socket"):
        value = tc.get(key)
        if isinstance(value, str) and value:
            socket = value
            break
    else:
        agent_depth = getattr(session, "agent_depth", 0)
        if isinstance(agent_depth, int) and agent_depth > 0:
            socket = "gobby"
        else:
            session_id = getattr(session, "id", None)
            socket = (
                f"session:{session_id}" if isinstance(session_id, str) else f"object:{id(session)}"
            )

    machine_id = str(getattr(session, "machine_id", "") or "").strip()
    return machine_id, socket, pane


def _tmux_repair_candidate_score(session: Any) -> tuple[int, int]:
    external_id = str(getattr(session, "external_id", "") or "").strip()
    has_identity = int(bool(external_id))
    has_activity = int(
        bool(str(getattr(session, "transcript_path", "") or "").strip())
        or bool(getattr(session, "message_count", 0))
        or bool(getattr(session, "turn_count", 0))
        or bool(getattr(session, "tool_call_count", 0))
    )
    return has_identity, has_activity


def _select_tmux_repair_sessions(sessions: Sequence[Any]) -> list[Any]:
    selected: dict[
        tuple[str, str, str],
        tuple[tuple[tuple[int, int], tuple[float, str]], Any],
    ] = {}

    for session in sessions:
        key = _tmux_repair_pane_key(session)
        if key is None:
            continue

        score = (
            _tmux_repair_candidate_score(session),
            terminal_session_creation_order(session),
        )
        current = selected.get(key)
        if current is None or score > current[0]:
            selected[key] = (score, session)

    return [session for _, session in selected.values()]
