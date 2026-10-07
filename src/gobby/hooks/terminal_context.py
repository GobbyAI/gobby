"""Hook terminal-context helpers."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import psutil

from gobby.sessions.tmux_context import parse_tmux_socket_path, query_tmux_identity

logger = logging.getLogger(__name__)

_DROID_PROCESS_NAME = "droid"
_CODEX_PROCESS_NAME = "codex"
_CODEX_APP_SERVER_ARG = "app-server"
_CODEX_SHARED_HOST_FLAG = "--managed-daemon"
_CODEX_RESUME_ARG = "resume"
_SEAT_RESCAN_INTERVAL_SECONDS = 30.0
# A fresh seat mints its thread within a second or two of starting; the window covers a
# slow start without reaching back to an unrelated TUI.
_FRESH_SEAT_WINDOW_SECONDS = 60.0

# Whether no Gobby session owns the seat TUI with this pid and create time. A new
# thread's registration supplies it from the database so a seat that sat at a login
# prompt past the fresh window can still be adopted when it is the only unowned one.
SeatAvailable = Callable[[int, float], bool]

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


def hook_sender_belongs_to_seat(sender_context: object, seat_context: object) -> bool:
    """Verify the raw hook sender descends from the recorded CLI process identity.

    A shared Codex host serves other seats even when this seat launched it.
    Missing or uninspectable sender identity cannot authorize ending a live seat.
    """
    if not isinstance(sender_context, Mapping) or not isinstance(seat_context, Mapping):
        return False
    sender_pid = sender_context.get("parent_pid")
    seat_pid = seat_context.get("parent_pid")
    seat_start = seat_context.get("parent_create_time")
    if (
        isinstance(sender_pid, bool)
        or not isinstance(sender_pid, (int, str))
        or isinstance(seat_pid, bool)
        or not isinstance(seat_pid, (int, str))
        or isinstance(seat_start, bool)
        or not isinstance(seat_start, (int, float, str))
    ):
        return False
    try:
        sender_pid, seat_pid, seat_start = int(sender_pid), int(seat_pid), float(seat_start)
        if sender_pid <= 0 or seat_pid <= 0:
            return False
        sender = psutil.Process(sender_pid)
        sender_start = sender_context.get("parent_create_time")
        if sender_start is not None:
            if isinstance(sender_start, bool) or not isinstance(sender_start, (int, float, str)):
                return False
            if not abs(float(sender.create_time()) - float(sender_start)) < 1.0:
                return False
        process: psutil.Process | None = sender
        while process is not None:
            if _is_codex_shared_host(process):
                return False
            if process.pid == seat_pid:
                return abs(float(process.create_time()) - seat_start) < 1.0
            process = process.parent()
    except (*_PSUTIL_ERRORS, ValueError, OverflowError):
        return False
    return False


def enrich_terminal_context_with_cwd(
    terminal_context: dict[str, Any] | None,
    cwd: Any,
    *,
    external_id: str | None = None,
    seat_available: SeatAvailable | None = None,
) -> dict[str, Any] | None:
    """Copy terminal context and add cwd and parent-process identity.

    ``external_id`` is the CLI's own session handle (the Codex thread id). A hook fired
    by a shared Codex app-server host is re-identified from that thread's seat process;
    ``seat_available`` widens a fresh thread's search to the one seat no session owns.
    """
    cwd_text = _non_empty_str(cwd)
    if terminal_context is None:
        return {"cwd": cwd_text} if cwd_text else None

    enriched = dict(terminal_context)
    if cwd_text and not _non_empty_str(enriched.get("cwd")):
        enriched["cwd"] = cwd_text
    _record_parent_process_identity(
        enriched,
        external_id=_non_empty_str(external_id),
        cwd=_non_empty_str(enriched.get("cwd")),
        seat_available=seat_available,
    )
    return enriched


def _record_parent_process_identity(
    terminal_context: dict[str, Any],
    *,
    external_id: str | None,
    cwd: str | None,
    seat_available: SeatAvailable | None,
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
            _replace_with_codex_seat_identity(
                terminal_context, hook_parent, external_id, cwd, seat_available
            )
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
    cwd: str | None,
    seat_available: SeatAvailable | None,
) -> None:
    """Swap the shared host's identity for the seat TUI that owns ``external_id``."""
    for key in _PROCESS_IDENTITY_KEYS:
        terminal_context.pop(key, None)
    seat = _SEAT_INDEX.resolve(external_id, cwd, seat_available) if external_id else None
    if seat is None:
        logger.warning(
            "Codex shared app-server host pid %s: no seat process resumes thread %s or "
            "started in the hook cwd just before it; recording no terminal identity",
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

    A resumed seat names its thread in argv (``codex resume <thread-id>``), the primary
    match. A fresh seat carries no thread in argv, but Codex thread ids are UUIDv7, so
    the thread's mint time is known: the newest seat TUI started in the hook's cwd at
    most ``_FRESH_SEAT_WINDOW_SECONDS`` before that time is adopted for the thread. A
    full process scan costs tens of milliseconds, so the index rescans only when a
    thread is unknown or its recorded process is gone, and rate-limits repeated misses
    for the same thread.
    """

    def __init__(self) -> None:
        self._seats: list[tuple[int, float, tuple[str, ...]]] = []
        self._adopted: dict[str, tuple[int, float]] = {}
        self._missed_at: dict[str, float] = {}

    def clear(self) -> None:
        self._seats = []
        self._adopted = {}
        self._missed_at = {}

    def resolve(
        self, thread_id: str, cwd: str | None, seat_available: SeatAvailable | None = None
    ) -> psutil.Process | None:
        seat = self._cached(thread_id)
        if seat is not None:
            return seat
        now = time.monotonic()
        missed_at = self._missed_at.get(thread_id)
        # Registration runs once per thread, so its wider search is never rate-limited.
        if (
            seat_available is None
            and missed_at is not None
            and now - missed_at < _SEAT_RESCAN_INTERVAL_SECONDS
        ):
            return None
        self._rescan()
        seat = self._cached(thread_id)
        if seat is None:
            seat = self._adopt(thread_id, cwd)
        if seat is None and seat_available is not None:
            seat = self._adopt_unowned(thread_id, cwd, seat_available)
        if seat is None:
            self._missed_at[thread_id] = now
        else:
            self._missed_at.pop(thread_id, None)
        return seat

    def _cached(self, thread_id: str) -> psutil.Process | None:
        for pid, create_time, cmdline in self._seats:
            if thread_id in cmdline:
                process = _recorded_process(pid, create_time)
                if process is not None:
                    return process
        adopted = self._adopted.get(thread_id)
        if adopted is not None:
            process = _recorded_process(*adopted)
            if process is not None:
                return process
            del self._adopted[thread_id]
        return None

    def _adopt(self, thread_id: str, cwd: str | None) -> psutil.Process | None:
        """Adopt the newest fresh seat TUI started just before ``thread_id`` was minted.

        Runs only on a fresh scan. The window is strictly earlier than the thread and
        deliberately narrow: a wrong seat sends keystrokes into another operator's
        terminal, so no identity beats a guess.
        """
        minted_at = _thread_minted_at(thread_id)
        if minted_at is None:
            return None
        for pid, create_time, cmdline in reversed(self._seats):
            if create_time > minted_at:
                continue
            if minted_at - create_time > _FRESH_SEAT_WINDOW_SECONDS:
                break
            if _CODEX_RESUME_ARG in cmdline:
                continue
            try:
                process = psutil.Process(pid)
                if not _is_fresh_seat_tui(process, cwd):
                    continue
            except _PSUTIL_ERRORS:
                continue
            self._adopted[thread_id] = (pid, create_time)
            return process
        return None

    def _adopt_unowned(
        self, thread_id: str, cwd: str | None, seat_available: SeatAvailable
    ) -> psutil.Process | None:
        """Adopt the only fresh seat TUI in ``cwd``, started before the thread, that no
        session owns.

        A seat can wait at a login prompt well past the fresh window before its first
        thread. Any second candidate refuses: a guess could type into another pane.
        """
        minted_at = _thread_minted_at(thread_id)
        if minted_at is None or cwd is None:
            return None
        candidates: list[tuple[psutil.Process, int, float]] = []
        for pid, create_time, cmdline in self._seats:
            if create_time > minted_at or _CODEX_RESUME_ARG in cmdline:
                continue
            try:
                process = psutil.Process(pid)
                if not _is_fresh_seat_tui(process, cwd):
                    continue
            except _PSUTIL_ERRORS:
                continue
            if seat_available(pid, create_time):
                candidates.append((process, pid, create_time))
        if len(candidates) != 1:
            return None
        process, pid, create_time = candidates[0]
        self._adopted[thread_id] = (pid, create_time)
        return process

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


def _recorded_process(pid: int, create_time: float) -> psutil.Process | None:
    """The live process behind a recorded pid and create time, or None once it is gone."""
    try:
        process = psutil.Process(pid)
        # Same tolerance as terminal_ownership.recorded_process_is_alive.
        if abs(process.create_time() - create_time) < 1.0:
            return process
    except _PSUTIL_ERRORS:
        pass
    return None


def _is_fresh_seat_tui(process: psutil.Process, cwd: str | None) -> bool:
    """A seat TUI is launched from a shell, never forked by another codex process, and
    runs in the hook's cwd when that cwd is known."""
    parent = process.parent()
    if parent is not None and parent.name() == _CODEX_PROCESS_NAME:
        return False
    if cwd is None:
        return True
    try:
        process_cwd: str = process.cwd()
    except _PSUTIL_ERRORS:
        return True
    return os.path.realpath(process_cwd) == os.path.realpath(cwd)


def _thread_minted_at(thread_id: str) -> float | None:
    """UNIX seconds at which a UUIDv7 thread id was minted; None for any other id."""
    try:
        parsed = UUID(thread_id)
    except ValueError:
        return None
    if parsed.version != 7:
        return None
    return (parsed.int >> 80) / 1000.0


_SEAT_INDEX = _CodexSeatIndex()


def clear_codex_seat_index() -> None:
    """Forget every cached seat process; tests call this between scenarios."""
    _SEAT_INDEX.clear()


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def hook_sandbox_enabled(input_data: Mapping[str, Any]) -> bool | None:
    """The launcher-supplied sandbox a session-start hook records, else None.

    Only a Gobby launcher's explicit bool counts. A pane is locked only under
    Gobby's SRT, whose launch and run records carry that boundary; a provider's
    own command-line sandbox never reads as locked.
    """
    raw = input_data.get("sandbox_enabled")
    return raw if isinstance(raw, bool) else None
