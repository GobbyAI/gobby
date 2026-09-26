"""Hook terminal-context helpers."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import psutil

from gobby.sessions.tmux_context import parse_tmux_socket_path, query_tmux_identity

logger = logging.getLogger(__name__)

_DROID_PROCESS_NAME = "droid"
_CODEX_PROCESS_NAME = "codex"
_CODEX_APP_SERVER_ARG = "app-server"
_CODEX_SHARED_HOST_FLAG = "--managed-daemon"
_SEAT_RESCAN_INTERVAL_SECONDS = 30.0

# Every key ghook derives from its own process and environment. Under a shared Codex
# app-server host those values describe the host, never the seat that fired the hook.
_PROCESS_IDENTITY_KEYS = (
    "parent_pid",
    "parent_create_time",
    "parent_name",
    "tty",
    "tmux_pane",
    "tmux_socket_path",
    "tmux_window_id",
    "tmux_session",
    "term_program",
    "gobby_session_id",
    "gobby_parent_session_id",
    "gobby_agent_run_id",
    "gobby_project_id",
    "gobby_workflow_name",
    "gobby_acp_child",
    "gobby_terminal_id",
    "gobby_pane_ref",
)
_ENVIRONMENT_IDENTITY_KEYS = (
    ("term_program", "TERM_PROGRAM"),
    ("gobby_session_id", "GOBBY_SESSION_ID"),
    ("gobby_parent_session_id", "GOBBY_PARENT_SESSION_ID"),
    ("gobby_agent_run_id", "GOBBY_AGENT_RUN_ID"),
    ("gobby_project_id", "GOBBY_PROJECT_ID"),
    ("gobby_workflow_name", "GOBBY_WORKFLOW_NAME"),
    ("gobby_acp_child", "GOBBY_ACP_CHILD"),
    ("gobby_terminal_id", "GOBBY_TERMINAL_ID"),
    ("gobby_pane_ref", "GOBBY_PANE_REF"),
)
_PSUTIL_ERRORS = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError)


def is_gobby_acp_child(terminal_context: object) -> bool:
    """Return whether terminal metadata marks a daemon-owned ACP child process."""
    return isinstance(terminal_context, Mapping) and terminal_context.get("gobby_acp_child") == "1"


def hook_cwd(data: Mapping[str, Any], event_cwd: Any = None) -> str | None:
    """Return the first non-empty cwd supplied by hook data or event metadata."""
    return _non_empty_str(data.get("cwd")) or _non_empty_str(event_cwd)


def enrich_terminal_context_with_cwd(
    terminal_context: dict[str, Any] | None,
    cwd: Any,
    *,
    external_id: str | None = None,
) -> dict[str, Any] | None:
    """Copy terminal context and add cwd and parent-process identity.

    ``external_id`` is the CLI's own session handle (the Codex thread id). A hook fired
    by a shared Codex app-server host is re-identified from that thread's seat process.
    """
    cwd_text = _non_empty_str(cwd)
    if terminal_context is None:
        return {"cwd": cwd_text} if cwd_text else None

    enriched = dict(terminal_context)
    if cwd_text and not _non_empty_str(enriched.get("cwd")):
        enriched["cwd"] = cwd_text
    _record_parent_process_identity(enriched, external_id=_non_empty_str(external_id))
    return enriched


def _record_parent_process_identity(
    terminal_context: dict[str, Any],
    *,
    external_id: str | None,
) -> None:
    terminal_context.pop("parent_create_time", None)
    terminal_context.pop("parent_name", None)

    parent_pid = terminal_context.get("parent_pid")
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, (int, str)):
        return
    try:
        pid = int(parent_pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return

    try:
        hook_parent = psutil.Process(pid)
        if _is_codex_shared_host(hook_parent):
            _replace_with_codex_seat_identity(terminal_context, hook_parent, external_id)
            return
        process = _stable_cli_process(hook_parent)
        if process is not hook_parent:
            terminal_context["parent_pid"] = process.pid
        terminal_context["parent_create_time"] = process.create_time()
        terminal_context["parent_name"] = process.name()
    except _PSUTIL_ERRORS:
        # An unverifiable pid is no identity: a CLI process's last hook as it exits would
        # otherwise merge its pid over the session's verified pid and create time.
        terminal_context.pop("parent_pid", None)
        terminal_context.pop("parent_create_time", None)
        terminal_context.pop("parent_name", None)


def _stable_cli_process(process: psutil.Process) -> psutil.Process:
    """Return the CLI process that outlives the session boundaries.

    Droid's TUI runs a ``droid exec`` backend child that runs the hooks, and it replaces
    that backend on ``/compress`` and ``/clear``. The TUI is the terminal's identity.
    A headless ``droid exec``, including one under another ``droid exec``, stays its own.
    """
    if process.name() != _DROID_PROCESS_NAME:
        return process
    try:
        parent = process.parent()
        if (
            parent is not None
            and parent.name() == _DROID_PROCESS_NAME
            and parent.cmdline()[1:2] != ["exec"]
        ):
            return parent
    except _PSUTIL_ERRORS:
        pass
    return process


def _is_codex_shared_host(process: psutil.Process) -> bool:
    """Return whether the hook's parent is Codex's shared managed app-server daemon.

    Codex 0.157 runs every seat's hooks inside one ``codex app-server --managed-daemon``
    process, so ghook's own environment is the daemon's. A ``codex app-server`` that
    Gobby spawned over stdio serves one seat and keeps its own environment.
    """
    return process.name() == _CODEX_PROCESS_NAME and _CODEX_SHARED_HOST_FLAG in process.cmdline()


def _replace_with_codex_seat_identity(
    terminal_context: dict[str, Any],
    host: psutil.Process,
    external_id: str | None,
) -> None:
    """Swap the shared host's identity for the seat TUI that owns ``external_id``."""
    for key in _PROCESS_IDENTITY_KEYS:
        terminal_context.pop(key, None)
    seat = _SEAT_INDEX.resolve(external_id) if external_id else None
    if seat is None:
        logger.warning(
            "Codex shared app-server host pid %s: no seat process resumes thread %s; "
            "recording no terminal identity",
            host.pid,
            external_id,
        )
        return
    try:
        terminal_context.update(_seat_identity(seat))
    except _PSUTIL_ERRORS:
        logger.warning(
            "Codex seat pid %s for thread %s is not inspectable; recording no terminal identity",
            seat.pid,
            external_id,
        )


def _seat_identity(seat: psutil.Process) -> dict[str, Any]:
    """Build the terminal context ghook would have built inside the seat process."""
    environ = seat.environ()
    identity: dict[str, Any] = dict.fromkeys(_PROCESS_IDENTITY_KEYS)
    identity["parent_pid"] = seat.pid
    identity["parent_create_time"] = seat.create_time()
    identity["parent_name"] = seat.name()
    identity["tty"] = _non_empty_str(seat.terminal())
    for context_key, environment_key in _ENVIRONMENT_IDENTITY_KEYS:
        identity[context_key] = _non_empty_str(environ.get(environment_key))
    tmux_pane = _non_empty_str(environ.get("TMUX_PANE"))
    tmux_socket_path = parse_tmux_socket_path(environ.get("TMUX"))
    if tmux_pane and tmux_socket_path:
        identity["tmux_pane"] = tmux_pane
        identity["tmux_socket_path"] = tmux_socket_path
        tmux_identity = query_tmux_identity(tmux_socket_path, tmux_pane)
        if tmux_identity is not None:
            identity["tmux_window_id"], identity["tmux_session"] = tmux_identity
    return identity


class _CodexSeatIndex:
    """Map Codex thread ids to their live seat TUI processes.

    Only a resumed seat names its thread in argv (``codex resume <thread-id>``). A full
    process scan costs tens of milliseconds, so the index rescans when a thread is
    unknown or its recorded process is gone, and rate-limits repeated misses for the
    same thread, which is what a fresh seat with no thread in argv produces on every hook.
    """

    def __init__(self) -> None:
        self._seats: list[tuple[int, float, tuple[str, ...]]] = []
        self._missed_at: dict[str, float] = {}

    def clear(self) -> None:
        self._seats = []
        self._missed_at = {}

    def resolve(self, thread_id: str) -> psutil.Process | None:
        seat = self._cached(thread_id)
        if seat is not None:
            return seat
        now = time.monotonic()
        missed_at = self._missed_at.get(thread_id)
        if missed_at is not None and now - missed_at < _SEAT_RESCAN_INTERVAL_SECONDS:
            return None
        self._rescan()
        seat = self._cached(thread_id)
        if seat is None:
            self._missed_at[thread_id] = now
        else:
            self._missed_at.pop(thread_id, None)
        return seat

    def _cached(self, thread_id: str) -> psutil.Process | None:
        for pid, create_time, cmdline in self._seats:
            if thread_id not in cmdline:
                continue
            try:
                process = psutil.Process(pid)
                # Same tolerance as terminal_ownership.recorded_process_is_alive.
                if abs(process.create_time() - create_time) < 1.0:
                    return process
            except _PSUTIL_ERRORS:
                pass
        return None

    def _rescan(self) -> None:
        seats: list[tuple[int, float, tuple[str, ...]]] = []
        for process in psutil.process_iter(attrs=["name"]):
            if process.info.get("name") != _CODEX_PROCESS_NAME:
                continue
            try:
                cmdline = tuple(process.cmdline())
                if _CODEX_APP_SERVER_ARG in cmdline:
                    continue
                seats.append((process.pid, process.create_time(), cmdline))
            except _PSUTIL_ERRORS:
                continue
        # The TUI is the oldest process naming a thread; helpers it forks come later.
        seats.sort(key=lambda seat: seat[1])
        self._seats = seats
        cutoff = time.monotonic() - _SEAT_RESCAN_INTERVAL_SECONDS
        self._missed_at = {
            thread_id: missed_at
            for thread_id, missed_at in self._missed_at.items()
            if missed_at >= cutoff
        }


_SEAT_INDEX = _CodexSeatIndex()


def clear_codex_seat_index() -> None:
    """Forget every cached seat process; tests call this between scenarios."""
    _SEAT_INDEX.clear()


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None
