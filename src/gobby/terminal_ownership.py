"""Canonical terminal-session identity and ownership ordering."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

import psutil

TerminalIdentity = tuple[str, str, str]
OwnershipReason = Literal[
    "validated_live_process",
    "nested_outermost_process",
    "ambiguous_live_processes",
    "single_lifecycle_fallback",
    "ambiguous_lifecycle_fallback",
    "ownerless",
    "invalid_identity",
]

_TMUX_SOCKET_FIELDS = (
    "tmux_socket_path",
    "tmux_socket_name",
    "tmux_socket",
)


class _ProcessLike(Protocol):
    pid: int

    def create_time(self) -> float: ...

    def parents(self) -> list[Any]: ...


@dataclass(frozen=True, slots=True)
class PaneOwnershipDecision:
    """One canonical decision for a physical tmux pane."""

    identity: TerminalIdentity | None
    requested_session_id: str | None
    owner: object | None
    reason: OwnershipReason
    validated_session_ids: frozenset[str] = frozenset()

    @property
    def owner_session_id(self) -> str | None:
        return _session_id(self.owner) if self.owner is not None else None

    @property
    def requested_session_owns_pane(self) -> bool:
        return bool(
            self.requested_session_id and self.owner_session_id == self.requested_session_id
        )


def _non_empty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _session_id(session: object) -> str | None:
    return _non_empty_text(getattr(session, "id", None) or getattr(session, "session_id", None))


def terminal_session_identity(session: object) -> TerminalIdentity | None:
    """Return machine/socket/pane identity when stored metadata is unambiguous."""
    terminal_context = getattr(session, "terminal_context", None)
    if not isinstance(terminal_context, Mapping):
        return None

    pane = _non_empty_text(terminal_context.get("tmux_pane"))
    if pane is None:
        return None

    socket_identity = None
    for field_name in _TMUX_SOCKET_FIELDS:
        socket_value = _non_empty_text(terminal_context.get(field_name))
        if socket_value is not None:
            socket_identity = f"{field_name}:{socket_value}"
            break
    if socket_identity is None:
        return None

    machine_id = _non_empty_text(getattr(session, "machine_id", None)) or ""
    return machine_id, socket_identity, pane


def terminal_session_creation_order(session: object) -> tuple[float, str]:
    """Return a deterministic immutable ordering key for terminal ownership."""
    created_at = getattr(session, "created_at", None)
    created_timestamp = (
        created_at.timestamp() if isinstance(created_at, datetime) else float("-inf")
    )

    session_id = _session_id(session) or ""
    return created_timestamp, session_id


def _normalized_parent_pid(session: object) -> int | None:
    terminal_context = getattr(session, "terminal_context", None)
    if not isinstance(terminal_context, Mapping):
        return None
    value = terminal_context.get("parent_pid")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _recorded_create_time(session: object) -> float | None:
    terminal_context = getattr(session, "terminal_context", None)
    if not isinstance(terminal_context, Mapping):
        return None
    value = terminal_context.get("parent_create_time")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validated_process(
    session: object,
    process_factory: Callable[[int], _ProcessLike],
) -> _ProcessLike | None:
    pid = _normalized_parent_pid(session)
    expected_create_time = _recorded_create_time(session)
    if pid is None or expected_create_time is None:
        return None
    try:
        process = process_factory(pid)
        if abs(process.create_time() - expected_create_time) >= 1.0:
            return None
        return process
    except (
        TypeError,
        ValueError,
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
        OSError,
    ):
        return None


def _process_ancestor_pids(process: _ProcessLike) -> set[int] | None:
    try:
        return {
            parent.pid
            for parent in process.parents()
            if isinstance(getattr(parent, "pid", None), int)
        }
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
        OSError,
    ):
        return None


def _newest_session(sessions: list[object]) -> object:
    return max(sessions, key=terminal_session_creation_order)


def resolve_pane_ownership(
    sessions: list[object],
    *,
    requested_session_id: str | None = None,
    process_factory: Callable[[int], _ProcessLike] = psutil.Process,
) -> PaneOwnershipDecision:
    """Select the process-backed owner of one physical tmux pane."""
    requested = _non_empty_text(requested_session_id)
    identities = {
        identity
        for session in sessions
        if (identity := terminal_session_identity(session)) is not None
    }
    if len(identities) != 1:
        return PaneOwnershipDecision(None, requested, None, "invalid_identity")
    identity = next(iter(identities))
    candidates = [session for session in sessions if terminal_session_identity(session) == identity]

    live_by_pid: dict[int, tuple[_ProcessLike, list[object]]] = {}
    validated_ids: set[str] = set()
    for session in candidates:
        process = _validated_process(session, process_factory)
        if process is None:
            continue
        session_id = _session_id(session)
        if session_id:
            validated_ids.add(session_id)
        group = live_by_pid.setdefault(process.pid, (process, []))[1]
        group.append(session)

    if len(live_by_pid) == 1:
        owner = _newest_session(next(iter(live_by_pid.values()))[1])
        return PaneOwnershipDecision(
            identity,
            requested,
            owner,
            "validated_live_process",
            frozenset(validated_ids),
        )

    if len(live_by_pid) > 1:
        ancestors = {
            pid: _process_ancestor_pids(process)
            for pid, (process, _sessions) in live_by_pid.items()
        }
        outermost = [
            pid
            for pid in live_by_pid
            if all(
                other_pid == pid or pid in (ancestors.get(other_pid) or set())
                for other_pid in live_by_pid
            )
        ]
        if len(outermost) == 1:
            owner = _newest_session(live_by_pid[outermost[0]][1])
            return PaneOwnershipDecision(
                identity,
                requested,
                owner,
                "nested_outermost_process",
                frozenset(validated_ids),
            )
        return PaneOwnershipDecision(
            identity,
            requested,
            None,
            "ambiguous_live_processes",
            frozenset(validated_ids),
        )

    fallback = [
        session
        for session in candidates
        if getattr(session, "status", None) in {"active", "paused"}
    ]
    if len(fallback) == 1:
        return PaneOwnershipDecision(
            identity,
            requested,
            fallback[0],
            "single_lifecycle_fallback",
        )
    reason: OwnershipReason = "ambiguous_lifecycle_fallback" if fallback else "ownerless"
    return PaneOwnershipDecision(identity, requested, None, reason)


def log_pane_ownership_decision(
    logger: logging.Logger,
    decision: PaneOwnershipDecision,
) -> None:
    """Emit one structured diagnostic for a pane ownership decision."""
    identity = decision.identity or ("", "", "")
    logger.debug(
        "Resolved tmux pane ownership: requested=%s owner=%s reason=%s",
        decision.requested_session_id,
        decision.owner_session_id,
        decision.reason,
        extra={
            "event": "tmux_pane_ownership_decision",
            "machine_id": identity[0],
            "tmux_socket": identity[1],
            "tmux_pane": identity[2],
            "requested_session_id": decision.requested_session_id,
            "terminal_owner_session_id": decision.owner_session_id,
            "decision_reason": decision.reason,
        },
    )
