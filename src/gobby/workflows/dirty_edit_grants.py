"""Grounds that let a session write dirt its own ledger does not cover.

A path is this session's own when the newest Gobby edit of it since the last
commit touching it belongs to a task this session holds (``tasks.claimed_by_session_id``).
An operator approval names exact paths in one checkout for
one session, and holds for each path only while it keeps the content it had
when approved. Only the operator route writes the reserved approval variable: the
daemon's dirty-path-approvals endpoint, which admits only a verified operator API key.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.git import run_git_command
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_checkout_root,
    task_checkout_edit_times,
    task_edit_path_covers,
    task_edited_file_set_with_history,
)

OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE = "operator_dirty_path_approvals"


def claimed_task_carriers(
    db: HubDatabase, *, session_id: str, checkout_root: str, paths: Iterable[str]
) -> tuple[frozenset[str], set[str]]:
    """Return the tasks this session holds and the ledger paths whose newest edit they own."""
    rows = db.fetchall(
        "SELECT id::text AS task_id FROM tasks WHERE claimed_by_session_id = %s",
        (session_id,),
    )
    own_tasks = frozenset(str(row["task_id"]) for row in rows)
    owners = checkout_path_owners(db, checkout_root=checkout_root, paths=paths)
    return own_tasks, {path for path, task_id in owners.items() if task_id in own_tasks}


def checkout_path_owners(
    db: HubDatabase, *, checkout_root: str, paths: Iterable[str]
) -> dict[str, str]:
    """Map each ledger path covering ``paths`` to the task whose newest edit still leaves it dirty.

    Every session's ledger counts. A task's Gobby edit is dirt only while it is newer than
    the last commit touching the path in this checkout, and among tasks that edited a path
    the newest edit owns it. Ledger key order never decides the owner.
    """
    root = normalize_task_checkout_root(checkout_root)
    wanted = set(paths)
    if root is None or not wanted:
        return {}
    rows = db.fetchall(
        """
        SELECT (session_variables.variables -> 'task_edited_file_checkouts')::text AS checkouts,
               (session_variables.variables -> 'task_edited_file_checkouts_history')::text
                   AS history_checkouts,
               (session_variables.variables -> 'task_edited_checkout_times')::text AS times
        FROM session_variables
        WHERE session_variables.variables ?| ARRAY[
            'task_edited_file_checkouts', 'task_edited_file_checkouts_history'
        ]
        """
    )
    newest: dict[str, dict[str, float]] = {}
    for row in rows:
        variables = {
            "task_edited_file_checkouts": _json_object(row["checkouts"]),
            "task_edited_file_checkouts_history": _json_object(row["history_checkouts"]),
            "task_edited_checkout_times": _json_object(row["times"]),
        }
        task_ids = {
            *variables["task_edited_file_checkouts"],
            *variables["task_edited_file_checkouts_history"],
        }
        for task_id in task_ids:
            stamps = task_checkout_edit_times(variables, task_id, root)
            for entry in task_edited_file_set_with_history(variables, task_id, root):
                if any(task_edit_path_covers(entry, path) for path in wanted):
                    by_task = newest.setdefault(entry, {})
                    by_task[task_id] = max(stamps.get(entry, 0.0), by_task.get(task_id, 0.0))
    owners: dict[str, str] = {}
    for entry, by_task in newest.items():
        task_id, stamp = max(by_task.items(), key=lambda item: (item[1], item[0]))
        last_commit = _last_commit_second(root, entry)
        # Git commit times are whole seconds, so an edit in the commit's own second is not
        # proven newer. Unstamped entries (stamp 0.0) own nothing once the path was committed.
        if last_commit is None or math.floor(stamp) > last_commit:
            owners[entry] = task_id
    return owners


def _last_commit_second(root: str, path: str) -> float | None:
    """Return the epoch second of the newest commit touching ``path``, or None if there is none.

    An unreadable history returns ``inf``, which proves no edit newer than the commit.
    """
    newest = run_git_command(
        ["git", "--literal-pathspecs", "log", "-1", "--format=%ct", "--", path],
        cwd=root,
        timeout=10,
    )
    if newest is None:
        unborn = run_git_command(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=root)
        return None if unborn is None else math.inf
    return float(newest) if newest else None


def _json_object(text: str | None) -> dict[str, Any]:
    value = json.loads(text) if text else None
    return value if isinstance(value, dict) else {}


def record_dirty_path_approval(
    db: HubDatabase,
    session_id: str,
    *,
    checkout_root: str,
    paths: Iterable[str],
    approved_by: str,
) -> dict[str, str | None]:
    """Approve exact paths for one session, replacing its approval for that checkout."""
    root_key = normalize_task_checkout_root(checkout_root)
    if root_key is None:
        raise ValueError(f"Checkout {checkout_root!r} is not an absolute path.")
    root = Path(root_key)
    fingerprints: dict[str, str | None] = {}
    for path in paths:
        rel = _checkout_relative(root, path)
        try:
            fingerprints[rel] = _fingerprint(root / rel)
        except OSError as exc:
            raise ValueError(f"{path} is not a file: {exc}") from exc
    if not fingerprints:
        raise ValueError("Name at least one path to approve.")
    manager = SessionVariableManager(db)
    stored = manager.get_variables(session_id).get(OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE)
    approvals = dict(stored) if isinstance(stored, dict) else {}
    approvals[root_key] = {
        "paths": dict(sorted(fingerprints.items())),
        "approved_by": approved_by,
        "approved_at": datetime.now(UTC).isoformat(),
    }
    manager.set_variable(session_id, OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE, approvals)
    return fingerprints


def approved_dirty_paths(variables: Mapping[str, Any], checkout_root: str) -> set[str]:
    """Return approved paths whose content still matches its approval."""
    root_key = normalize_task_checkout_root(checkout_root)
    approvals = variables.get(OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE)
    if root_key is None or not isinstance(approvals, dict):
        return set()
    approval = approvals.get(root_key)
    paths = approval.get("paths") if isinstance(approval, dict) else None
    if not isinstance(paths, dict):
        return set()
    approved: set[str] = set()
    for rel, expected in paths.items():
        try:
            if _fingerprint(Path(root_key) / rel) == expected:
                approved.add(rel)
        except OSError:
            continue
    return approved


def dirty_path_approval_command(session_id: str, checkout_root: str, paths: Iterable[str]) -> str:
    """The operator command that approves `paths` for this session."""
    root = normalize_task_checkout_root(checkout_root) or checkout_root
    return shlex.join(
        ["gobby", "sessions", "approve-dirty-paths", session_id, "--checkout", root, *sorted(paths)]
    )


def _checkout_relative(root: Path, path: str) -> str:
    joined = Path(os.path.normpath(root / path))
    try:
        rel = (joined.parent.resolve(strict=False) / joined.name).relative_to(root)
    except ValueError:
        raise ValueError(f"{path!r} is outside checkout {root}.") from None
    if not path.strip() or rel == Path("."):
        raise ValueError(f"{path!r} does not name a path inside checkout {root}.")
    return rel.as_posix()


def _fingerprint(path: Path) -> str | None:
    """Git's blob hash of the path's content, or None when the path is absent."""
    try:
        data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()
