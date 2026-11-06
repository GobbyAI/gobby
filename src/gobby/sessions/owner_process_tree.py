"""Stop the jobs a background call recorded, and name the calls whose recorded jobs are gone.

A background call's job is identified when its receipt arrives, while the owner's process is
still its parent: each group and session that a descendant leads is recorded with the leader's
create time. A graceful owner end then stops only those recorded groups and sessions, and a call
passes only when every group and session recorded for it is empty afterwards.

A recorded leader that is gone still holds its group while any member runs, because a pid is not
reused while it names a live group or session. A recorded leader alive with a different create
time is a reused pid: its group is gone, and its process is neither a member nor a target. A job
whose shell exited is reparented, but it keeps the shell's group, so it is still found.

Residual: a process that moves itself into a new group or session escapes the sweep.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import psutil

from gobby.sessions.terminal_kill import create_time_matches, process_identity_matches

_POLL_SECONDS = 0.05
_KILL_WAIT_SECONDS = 2.0
_GRACE_SECONDS = 3.0


@dataclass(frozen=True)
class JobTree:
    """The groups and sessions a background call's job led when its receipt arrived.

    Each entry is a leader's pid with its create time, the record that a later end verifies.
    """

    groups: tuple[tuple[int, float], ...]
    sessions: tuple[tuple[int, float], ...]

    def record(self) -> dict[str, dict[str, float]]:
        """The tree as JSON-safe session state."""
        return {
            "groups": {str(pid): created for pid, created in self.groups},
            "sessions": {str(pid): created for pid, created in self.sessions},
        }


def job_tree_of(value: Any) -> JobTree | None:
    """Parse a stored tree; None unless it has the recorded shape and names a leader."""
    if not isinstance(value, dict):
        return None
    groups = _leaders(value.get("groups"))
    sessions = _leaders(value.get("sessions"))
    if groups is None or sessions is None or not (groups or sessions):
        return None
    return JobTree(groups, sessions)


def capture_job_tree(terminal_context: Mapping[str, Any] | None) -> JobTree | None:
    """Record the groups and sessions the owner's live jobs lead; None when none can be named.

    Read once, when a background call's receipt arrives. The owner must still be the process the
    session recorded. The owner and this process are never recorded, and a job that shares the
    owner's own group names no job of its own.
    """
    context: Mapping[str, Any] = terminal_context or {}
    pid = _owner_pid(context.get("parent_pid"))
    if pid is None or not process_identity_matches(pid, context):
        return None
    groups: dict[int, float] = {}
    sessions: dict[int, float] = {}
    try:
        for process in psutil.Process(pid).children(recursive=True):
            if process.pid == os.getpid():
                continue
            try:
                group = os.getpgid(process.pid)
                session = os.getsid(process.pid)
            except ProcessLookupError:
                continue
            if group == process.pid:
                groups[process.pid] = process.create_time()
            if session == process.pid:
                sessions[process.pid] = process.create_time()
    except (psutil.Error, OSError):
        return None
    if not groups and not sessions:
        return None
    return JobTree(tuple(sorted(groups.items())), tuple(sorted(sessions.items())))


def stop_recorded_job_trees(
    terminal_context: Mapping[str, Any] | None, trees: Mapping[str, JobTree]
) -> frozenset[str]:
    """Stop the recorded jobs, then name the calls whose recorded jobs are all gone.

    Fail-closed: a member that cannot be read, a leader whose identity cannot be checked, or a
    survivor keeps the calls that own it foreign. The owner and this process are never signalled.
    """
    if not trees:
        return frozenset()
    context: Mapping[str, Any] = terminal_context or {}
    skip = {os.getpid()}
    owner = _owner_pid(context.get("parent_pid"))
    if owner is not None:
        skip.add(owner)
    try:
        held = {call_id: _held(tree) for call_id, tree in trees.items()}
        members = _live_members(held.values(), skip)
        _signal(members, kill=False)
        remaining = _wait_until_gone(members, _GRACE_SECONDS)
        if remaining:
            _signal(remaining, kill=True)
            _wait_until_gone(remaining, _KILL_WAIT_SECONDS)
        return frozenset(
            call_id
            for call_id, (groups, sessions) in held.items()
            if not _live(_members(groups, sessions, skip))
        )
    except (psutil.Error, OSError):
        return frozenset()


def _leaders(value: Any) -> tuple[tuple[int, float], ...] | None:
    if not isinstance(value, dict):
        return None
    leaders: list[tuple[int, float]] = []
    for key, created in value.items():
        if not isinstance(key, str) or not key.isdigit() or int(key) <= 0:
            return None
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            return None
        leaders.append((int(key), float(created)))
    return tuple(sorted(leaders))


def _held(tree: JobTree) -> tuple[frozenset[int], frozenset[int]]:
    """The recorded groups and sessions that still hold their jobs: leader gone, or the same."""
    return (
        frozenset(pid for pid, created in tree.groups if _holds(pid, created)),
        frozenset(pid for pid, created in tree.sessions if _holds(pid, created)),
    )


def _holds(pid: int, created: float) -> bool:
    try:
        return create_time_matches(psutil.Process(pid).create_time(), created)
    except psutil.NoSuchProcess:
        # A gone leader's pid is not reused while a member of its group or session runs.
        return True


def _live_members(
    held: Iterable[tuple[frozenset[int], frozenset[int]]], skip: set[int]
) -> list[psutil.Process]:
    members: dict[int, psutil.Process] = {}
    for groups, sessions in held:
        for process in _live(_members(groups, sessions, skip)):
            members[process.pid] = process
    return list(members.values())


def _members(
    groups: frozenset[int], sessions: frozenset[int], skip: set[int]
) -> list[psutil.Process]:
    """Processes in one of the groups or sessions, other than ``skip``."""
    if not groups and not sessions:
        return []
    found: list[psutil.Process] = []
    for process in psutil.process_iter():
        # process_iter yields the kernel's pid 0 on macOS; it never belongs to a job.
        if process.pid in skip or process.pid == 0:
            continue
        try:
            if os.getpgid(process.pid) in groups or os.getsid(process.pid) in sessions:
                found.append(process)
        except ProcessLookupError:
            continue
    return found


def _owner_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _signal(processes: list[psutil.Process], *, kill: bool) -> None:
    for process in processes:
        try:
            if kill:
                process.kill()
            else:
                process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # A gone process needs nothing. One the kernel refuses to signal stays live, so its
            # call stays foreign, and the sweep still reaches the rest of the recorded jobs.
            continue


def _live(processes: list[psutil.Process]) -> list[psutil.Process]:
    """Processes that still run; an exited process whose parent has not reaped it is gone."""
    live: list[psutil.Process] = []
    for process in processes:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                live.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            live.append(process)
    return live


def _wait_until_gone(processes: list[psutil.Process], seconds: float) -> list[psutil.Process]:
    deadline = time.monotonic() + seconds
    remaining = _live(processes)
    while remaining and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        remaining = _live(remaining)
    return remaining
