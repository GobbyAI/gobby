"""Attribute a session's Bash writes to that session inside its own linked task worktree.

An interpreter script (``uv run python x.py``) can write any file, so its writes cannot be
named from the command text. In a linked worktree that a claimed task owns, the hook diffs
the worktree's dirty set and each dirty file's digest across the call, and attributes what
changed to the calling session. Main and shared checkouts stay unattributed.

Each session keeps its in-flight calls in its own ``worktree_shell_calls`` variable and the
paths that overlapping calls changed in ``worktree_shell_overlaps``. A call commits its entry
before it scans the other sessions, and every scan marks each overlapping entry, so two calls
that run at once cannot both miss each other. A structured edit is in flight from before the
rules judge it until its write completes or is refused, so a Bash call that overlaps the write
marks it too. A path an overlapping call changed stays foreign to every session.

A background Bash call stays in flight until its output file shows that the process ended. Its
owner settles it before the next tool call of that session is judged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from gobby.hooks.events import tool_execution_id
from gobby.utils.daemon_git import GitOk, daemon_git, parse_porcelain_v1_z
from gobby.workflows.checkout_fingerprint import path_fingerprints

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import LocalTaskManager
    from gobby.workflows.state_manager import SessionVariableManager


class _HadEditsMarker(Protocol):
    """The one session-manager call that flags a session as having edits."""

    def mark_had_edits(self, session_id: str) -> object: ...


CALLS_VARIABLE = "worktree_shell_calls"
OVERLAPS_VARIABLE = "worktree_shell_overlaps"
# A foreground Bash call cannot run longer than 10 minutes, so an older entry belongs to a
# call whose after-hook never ran and must not mark a live call as overlapping.
STALE_CALL_SECONDS = 600.0
# A background call stays in flight while its output file exists. This ceiling only bounds an
# output file whose end line never came, so it cannot mark live calls forever.
BACKGROUND_STALE_SECONDS = 4 * 60 * 60.0
_BACKGROUND_TAIL_BYTES = 512
_BACKGROUND_END_LINE = re.compile(r"\[(?:exited with code -?\d+|killed)\]")
# Call ids of structured edits; a Bash call's id is its tool-call id, which never has this prefix.
_EDIT_CALL_PREFIX = "edit:"


@dataclass(frozen=True)
class SettledCall:
    """What one call left in its worktree, judged against the other sessions' calls."""

    overlapped: bool
    changed: tuple[str, ...]
    attributable: bool
    task_started_at: float | None = None


async def worktree_digests(root: str) -> dict[str, str | None] | None:
    """Digest every path git reports dirty under ``root``; None when git cannot report them."""
    result = await daemon_git.status(root, timeout=10.0)
    if not isinstance(result, GitOk):
        return None
    try:
        entries = parse_porcelain_v1_z(result.stdout)
    except ValueError:
        return None
    paths = {path for entry in entries for path in (entry.path, entry.original_path) if path}
    fingerprints = path_fingerprints(Path(root), paths)
    return {path: fingerprints.get(path) for path in paths}


def changed_paths(before: Mapping[str, str | None], after: Mapping[str, str | None]) -> list[str]:
    """Paths dirty in one snapshot and not the other, or dirty in both with different digests.

    A clean path is absent from a snapshot, and a deleted one maps to None, so presence is part
    of the comparison: a clean tracked file that a call deletes changed.
    """
    return sorted(
        path
        for path in before.keys() | after.keys()
        if (path in before) != (path in after) or before[path] != after[path]
    )


def is_registered_worktree(db: HubDatabase, project_id: str | None, root: str) -> bool:
    """Whether ``root`` is a linked worktree registered for ``project_id``."""
    if project_id is None:
        return False
    from gobby.storage.worktrees import LocalWorktreeManager

    rows = LocalWorktreeManager(db).list_worktrees(project_id=project_id, limit=None)
    return any(os.path.realpath(row.worktree_path) == root for row in rows)


def overlapped_worktree_paths(variables: Mapping[str, Any], root: str) -> frozenset[str]:
    """Paths an overlapping call of this session changed in ``root``; they stay foreign."""
    records = variables.get(OVERLAPS_VARIABLE)
    if not isinstance(records, dict):
        return frozenset()
    paths = records.get(root)
    if not isinstance(paths, list):
        return frozenset()
    return frozenset(path for path in paths if isinstance(path, str))


def structured_edit_paths(data: Mapping[str, Any], *, is_canonical: bool) -> list[str]:
    """The repository paths a structured edit names, from its canonical or legacy fields."""
    canonical = data.get("canonical_edit_file_paths", data.get("canonical_file_paths"))
    if is_canonical and isinstance(canonical, list):
        return list(dict.fromkeys(path for path in canonical if isinstance(path, str) and path))
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    legacy = tool_input.get("file_path") or tool_input.get("target_file") or tool_input.get("path")
    return [legacy] if isinstance(legacy, str) and legacy else []


def edit_call_key(request_id: str, root: str) -> str:
    """The call id a structured edit registers under in one worktree."""
    return f"{_EDIT_CALL_PREFIX}{request_id}:{root}"


def _calls_of(variables: Mapping[str, Any]) -> dict[str, Any]:
    value = variables.get(CALLS_VARIABLE)
    return dict(value) if isinstance(value, dict) else {}


def _started_at_of(entry: Mapping[str, Any]) -> float | None:
    value = entry.get("task_started_at")
    return float(value) if isinstance(value, int | float) else None


def background_output_state(output_path: str) -> str:
    """Return ``running``, ``finished`` or ``missing`` for a background call's output file.

    The CLI ends the file with an ``[exited with code N]`` or ``[killed]`` line when the
    process stops.
    """
    path = Path(output_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - _BACKGROUND_TAIL_BYTES))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return "missing"
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return "finished" if lines and _BACKGROUND_END_LINE.fullmatch(lines[-1]) else "running"


def _is_fresh(entry: Any, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    age = now - float(entry.get("started_at", 0.0))
    output_path = entry.get("output_path")
    if entry.get("pending_receipt") is True:
        return age < BACKGROUND_STALE_SECONDS
    if isinstance(output_path, str):
        return age < BACKGROUND_STALE_SECONDS and background_output_state(output_path) != "missing"
    return age < STALE_CALL_SECONDS


def _json_object(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class WorktreeShellCallRegistry(ABC):
    """Tracks each session's in-flight calls per worktree on the session rows."""

    db: HubDatabase

    @abstractmethod
    def _mutate_variables(
        self,
        session_id: str,
        mutator: Callable[[dict[str, Any]], tuple[Any, bool]],
        *,
        keys: Iterable[str],
        apply_defaults: bool = False,
    ) -> Any:
        raise NotImplementedError

    @abstractmethod
    def get_variable_subset(self, session_id: str, keys: Iterable[str]) -> dict[str, Any]:
        raise NotImplementedError

    def begin_worktree_shell_call(
        self,
        session_id: str,
        root: str,
        call_id: str,
        *,
        before: Mapping[str, str | None],
        attributable: bool,
        now: float,
        task_started_at: float | None = None,
    ) -> None:
        """Register one call before it runs, then mark every in-flight call it overlaps."""
        entry = {
            "root": root,
            "started_at": now,
            "attributable": attributable,
            "overlapped": False,
            "dirty": dict(before),
            "task_started_at": task_started_at,
        }

        def add(variables: dict[str, Any]) -> tuple[None, bool]:
            calls = {
                key: value for key, value in _calls_of(variables).items() if _is_fresh(value, now)
            }
            calls[call_id] = entry
            variables[CALLS_VARIABLE] = calls
            return None, True

        self._mutate_variables(session_id, add, keys=(CALLS_VARIABLE,))
        hits = self._in_flight_elsewhere(session_id, root, now)
        for other_session_id, other_call_id in hits:
            self._mark_overlapped(other_session_id, other_call_id)
        if hits:
            self._mark_overlapped(session_id, call_id)

    def defer_worktree_shell_call(
        self, session_id: str, call_id: str, *, output_path: str | None
    ) -> None:
        """Keep a call in flight until its output file or terminal poll proves it ended."""

        def defer(variables: dict[str, Any]) -> tuple[None, bool]:
            calls = _calls_of(variables)
            entry = calls.get(call_id)
            if not isinstance(entry, dict):
                return None, False
            calls[call_id] = {
                **entry,
                "output_path": output_path,
                "pending_receipt": output_path is None,
            }
            variables[CALLS_VARIABLE] = calls
            return None, True

        self._mutate_variables(session_id, defer, keys=(CALLS_VARIABLE,))

    def worktree_shell_call_entries(self, session_id: str) -> dict[str, dict[str, Any]]:
        """This session's registered calls by call id, as stored, stale ones included."""
        calls = _calls_of(self.get_variable_subset(session_id, (CALLS_VARIABLE,)))
        return {key: entry for key, entry in calls.items() if isinstance(entry, dict)}

    def background_worktree_calls(self, session_id: str) -> dict[str, dict[str, Any]]:
        """This session's registered calls that were deferred to a background output file."""
        return {
            key: entry
            for key, entry in self.worktree_shell_call_entries(session_id).items()
            if isinstance(entry.get("output_path"), str)
        }

    def worktree_shell_call_root(self, session_id: str, call_id: str) -> str | None:
        """The worktree a registered call runs in, or None when the call is not registered."""
        entry = self.worktree_shell_call_entries(session_id).get(call_id)
        root = entry.get("root") if entry is not None else None
        return root if isinstance(root, str) else None

    def settle_worktree_shell_call(
        self,
        session_id: str,
        root: str,
        call_id: str,
        *,
        after: Mapping[str, str | None] | None,
        now: float,
    ) -> SettledCall | None:
        """Retire one call and report what it changed; None when the call was not registered.

        The scan runs before the pop, so this call stays visible to every call that begins
        or ends meanwhile, and each of those marks it. Without a readable ``after`` snapshot
        nothing is attributed.
        """
        hits = self._in_flight_elsewhere(session_id, root, now)
        for other_session_id, other_call_id in hits:
            self._mark_overlapped(other_session_id, other_call_id)

        def pop(variables: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
            calls = _calls_of(variables)
            if call_id not in calls:
                return None, False
            entry = calls.pop(call_id)
            variables[CALLS_VARIABLE] = calls or None
            return entry, True

        entry = self._mutate_variables(session_id, pop, keys=(CALLS_VARIABLE,))
        if not isinstance(entry, dict):
            return None
        overlapped = bool(hits) or bool(entry.get("overlapped"))
        attributable = bool(entry.get("attributable"))
        task_started_at = _started_at_of(entry)
        if after is None:
            return SettledCall(
                overlapped=overlapped,
                changed=(),
                attributable=False,
                task_started_at=task_started_at,
            )
        changed = changed_paths(entry.get("dirty") or {}, after)
        self._settle_overlaps(session_id, root, changed, overlapped, after)
        return SettledCall(
            overlapped=overlapped,
            changed=tuple(changed),
            attributable=attributable,
            task_started_at=task_started_at,
        )

    def _in_flight_elsewhere(self, session_id: str, root: str, now: float) -> list[tuple[str, str]]:
        """Other sessions' fresh in-flight calls in ``root``, as (session id, call id) pairs."""
        rows = self.db.fetchall(
            """
            SELECT session_variables.session_id AS session_id,
                   (session_variables.variables -> 'worktree_shell_calls')::text AS calls
            FROM session_variables
            WHERE session_variables.variables ? 'worktree_shell_calls'
              AND session_variables.session_id <> %s
            """,
            (session_id,),
        )
        hits: list[tuple[str, str]] = []
        for row in rows:
            for other_call_id, entry in _json_object(row["calls"]).items():
                if _is_fresh(entry, now) and entry.get("root") == root:
                    hits.append((str(row["session_id"]), other_call_id))
        return hits

    def _mark_overlapped(self, session_id: str, call_id: str) -> None:
        def mark(variables: dict[str, Any]) -> tuple[None, bool]:
            calls = _calls_of(variables)
            entry = calls.get(call_id)
            if not isinstance(entry, dict) or entry.get("overlapped"):
                return None, False
            calls[call_id] = {**entry, "overlapped": True}
            variables[CALLS_VARIABLE] = calls
            return None, True

        self._mutate_variables(session_id, mark, keys=(CALLS_VARIABLE,))

    def _settle_overlaps(
        self,
        session_id: str,
        root: str,
        changed: list[str],
        overlapped: bool,
        after: Mapping[str, str | None],
    ) -> None:
        """Keep the root's overlap record to paths still dirty, adding this call's if overlapped."""

        def settle(variables: dict[str, Any]) -> tuple[None, bool]:
            raw = variables.get(OVERLAPS_VARIABLE)
            records = dict(raw) if isinstance(raw, dict) else {}
            kept = {path for path in records.get(root, []) if isinstance(path, str)}
            if overlapped:
                kept |= set(changed)
            kept = {path for path in kept if path in after}
            if kept:
                records[root] = sorted(kept)
            else:
                records.pop(root, None)
            variables[OVERLAPS_VARIABLE] = records or None
            return None, True

        self._mutate_variables(session_id, settle, keys=(OVERLAPS_VARIABLE,))

    def clear_overlapped_paths(self, session_id: str, root: str, paths: Collection[str]) -> None:
        """Drop paths a native structured write named from this session's overlap record."""
        named = set(paths)

        def clear(variables: dict[str, Any]) -> tuple[None, bool]:
            raw = variables.get(OVERLAPS_VARIABLE)
            records = dict(raw) if isinstance(raw, dict) else {}
            kept = sorted(path for path in records.get(root, []) if path not in named)
            if kept:
                records[root] = kept
            else:
                records.pop(root, None)
            variables[OVERLAPS_VARIABLE] = records or None
            return None, True

        self._mutate_variables(session_id, clear, keys=(OVERLAPS_VARIABLE,))


def attribute_settled_call(
    manager: SessionVariableManager,
    session_id: str,
    root: str,
    settled: SettledCall | None,
    *,
    edited_at: float,
) -> list[str]:
    """Record the paths a settled call changed for its session; return the paths recorded.

    A call that overlapped another session's call, a call that is not attributable, and a path
    another live session holds record nothing.
    """
    from gobby.workflows.commit_guard import live_session_held_paths

    if settled is None or settled.overlapped or not settled.attributable or not settled.changed:
        return []
    held = live_session_held_paths(
        manager.db,
        checkout_root=root,
        paths=set(settled.changed),
        exclude_session_id=session_id,
    )
    paths = [path for path in settled.changed if path not in held]
    if not paths:
        return []
    manager.record_edited_files(
        session_id,
        paths,
        checkout_root=root,
        edited_at=edited_at,
        started_at=settled.task_started_at,
        attribute_to_task=settled.task_started_at is not None,
    )
    return paths


async def settle_background_worktree_shell_calls(
    manager: SessionVariableManager, session_id: str
) -> bool:
    """Settle this session's background calls whose process ended; True when paths were recorded.

    A running call stays in flight. A call whose output file is gone is retired without
    attribution, so its paths stay foreign. Database work runs in worker threads; the digest runs
    on the event loop.
    """
    now = time.time()
    recorded = False
    for call_id, entry in (
        await asyncio.to_thread(manager.background_worktree_calls, session_id)
    ).items():
        root = entry.get("root")
        output_path = entry.get("output_path")
        if not isinstance(root, str) or not isinstance(output_path, str):
            continue
        state = await asyncio.to_thread(background_output_state, output_path)
        if state == "running":
            continue
        after = await worktree_digests(root) if state == "finished" else None
        settled = await asyncio.to_thread(
            manager.settle_worktree_shell_call, session_id, root, call_id, after=after, now=now
        )
        paths = await asyncio.to_thread(
            attribute_settled_call, manager, session_id, root, settled, edited_at=time.time()
        )
        recorded = recorded or bool(paths)
    return recorded


def begin_structured_edit_calls(
    manager: SessionVariableManager, session_id: str, event: HookEvent
) -> None:
    """Register a structured edit as in flight in each registered linked worktree it writes.

    Runs before the rules judge the edit, so a Bash call that settles after that judgment finds
    the edit in flight. The edit attributes nothing; it only marks overlapping Bash calls.
    """
    from gobby.hooks._normalization_canonical import CANONICAL_WRITE_TOOL_NAMES
    from gobby.workflows.task_claim_state import (
        effective_tool_cwd,
        normalize_task_checkout_root,
        resolve_edit_pair,
    )

    data = event.data
    request_id = tool_execution_id(event)
    project_id = event.project_id
    if request_id is None or project_id is None or not isinstance(data, dict):
        return
    tool_name = str(data.get("tool_name", "")).lower()
    if tool_name == "bash":
        return
    is_canonical = (
        data.get("canonical_tool_kind") == "write" and data.get("canonical_repo_mutation") is True
    )
    if tool_name not in CANONICAL_WRITE_TOOL_NAMES and not is_canonical:
        return
    file_paths = structured_edit_paths(data, is_canonical=is_canonical)
    if not file_paths:
        return
    variables = manager.get_variables(session_id)
    cwd = effective_tool_cwd(event.cwd, data)
    roots: list[str] = []
    for file_path in file_paths:
        pair = resolve_edit_pair(
            file_path, cwd, project_id=project_id, db=manager.db, variables=variables
        )
        root = normalize_task_checkout_root(pair[0]) if pair is not None else None
        if root is not None and root not in roots:
            roots.append(root)
    now = time.time()
    for root in roots:
        if is_registered_worktree(manager.db, project_id, root):
            manager.begin_worktree_shell_call(
                session_id,
                root,
                edit_call_key(request_id, root),
                before={},
                attributable=False,
                now=now,
            )


def release_structured_edit_calls(
    manager: SessionVariableManager, session_id: str, request_id: str
) -> None:
    """Retire this request's structured-edit entries once its write completed or was refused."""
    prefix = f"{_EDIT_CALL_PREFIX}{request_id}:"
    now = time.time()
    for call_id, entry in manager.worktree_shell_call_entries(session_id).items():
        root = entry.get("root")
        if call_id.startswith(prefix) and isinstance(root, str):
            manager.settle_worktree_shell_call(session_id, root, call_id, after=None, now=now)


def mark_session_had_edits_if_claimed(
    task_manager: LocalTaskManager | None,
    session_manager: _HadEditsMarker | None,
    session_id: str,
    logger: logging.Logger,
) -> None:
    """Flag the session as having edits when it holds a claimed task."""
    if task_manager is None or session_manager is None:
        return
    try:
        claimed_tasks = task_manager.list_tasks(claimed_by_session_id=session_id)
    except Exception as exc:
        logger.debug("Failed to check claimed tasks for session %s: %s", session_id, exc)
        return
    if claimed_tasks:
        session_manager.mark_had_edits(session_id)
