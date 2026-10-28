"""Grounds that let a session write dirt its own ledger does not cover.

A task owns a path while the path still holds the content its latest Gobby action
left: each record stores the path's git blob hash, and a match with the current
content is the proof. Any change made outside Gobby breaks the match, and no commit
time or clock enters the test. An operator approval names exact paths in one checkout
for one session, and holds for each path only while it keeps the content it had
when approved. Only the operator route writes the reserved approval variable: the
daemon's dirty-path-approvals endpoint, which admits only a verified operator API key.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.checkout_fingerprint import file_fingerprint, path_fingerprints
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_checkout_root,
    task_checkout_edit_hashes,
    task_checkout_edit_times,
)

OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE = "operator_dirty_path_approvals"
# Never a git blob hash, so an unreadable path matches no record.
_UNREADABLE = "unreadable"


def claimed_task_carriers(
    db: HubDatabase, *, session_id: str, checkout_root: str, paths: Iterable[str]
) -> tuple[frozenset[str], set[str]]:
    """Return the tasks this session holds and the ledger paths whose newest edit they own."""
    own_tasks = _claimed_task_ids(db, session_id)
    owners = checkout_path_owners(db, checkout_root=checkout_root, paths=paths)
    return own_tasks, {path for path, task_id in owners.items() if task_id in own_tasks}


def claimed_task_owned_paths(
    db: HubDatabase, *, session_id: str, checkout_root: str
) -> dict[str, tuple[str, str | None]]:
    """Return each ledger path a claimed task owns now, as ``(task_id, hash now)``.

    A path counts only while its current content matches a hash one of the session's claimed
    tasks recorded, so a write made outside Gobby over that content owns nothing.
    """
    root = normalize_task_checkout_root(checkout_root)
    own_tasks = _claimed_task_ids(db, session_id)
    if root is None or not own_tasks:
        return {}
    ledger = claimed_task_ledger_paths(db, task_ids=own_tasks, checkout_root=root)
    owners = checkout_path_owners(
        db, checkout_root=root, paths={path for paths in ledger.values() for path in paths}
    )
    owned = sorted(path for path, task_id in owners.items() if task_id in own_tasks)
    digests = path_fingerprints(Path(root), owned)
    return {path: (owners[path], digests[path]) for path in owned if path in digests}


def _claimed_task_ids(db: HubDatabase, session_id: str) -> frozenset[str]:
    rows = db.fetchall(
        "SELECT id::text AS task_id FROM tasks WHERE claimed_by_session_id = %s",
        (session_id,),
    )
    return frozenset(str(row["task_id"]) for row in rows)


def claimed_task_ledger_paths(
    db: HubDatabase, *, task_ids: Iterable[str], checkout_root: str
) -> dict[str, set[str]]:
    """Return, per task, every path any session's ledger recorded for it in one checkout."""
    root = normalize_task_checkout_root(checkout_root)
    tasks = sorted(set(task_ids))
    if root is None or not tasks:
        return {}
    rows = db.fetchall(
        """
        SELECT (session_variables.variables -> 'task_edited_checkout_hashes')::text AS hashes
        FROM session_variables
        WHERE (session_variables.variables -> 'task_edited_checkout_hashes') ?| %s
        """,
        (tasks,),
    )
    paths: dict[str, set[str]] = {task_id: set() for task_id in tasks}
    for row in rows:
        variables = {"task_edited_checkout_hashes": _json_object(row["hashes"])}
        for task_id in tasks:
            paths[task_id].update(task_checkout_edit_hashes(variables, task_id, root))
    return paths


def checkout_path_owners(
    db: HubDatabase, *, checkout_root: str, paths: Iterable[str]
) -> dict[str, str]:
    """Map each ledger path among ``paths`` to the task whose recorded content it still holds.

    A record counts while the path's current content matches the hash it had after that
    task's Gobby action. Among matching records the newest edit owns the path, and ledger
    key order never decides the owner. Every session's ledger counts.
    """
    root = normalize_task_checkout_root(checkout_root)
    wanted = set(paths)
    if root is None or not wanted:
        return {}
    rows = db.fetchall(
        """
        SELECT (session_variables.variables -> 'task_edited_checkout_hashes')::text AS hashes,
               (session_variables.variables -> 'task_edited_checkout_times')::text AS times
        FROM session_variables
        WHERE session_variables.variables ? 'task_edited_checkout_hashes'
        """
    )
    current: dict[str, str | None] = {}
    newest: dict[str, tuple[float, str]] = {}
    for row in rows:
        variables = {
            "task_edited_checkout_hashes": _json_object(row["hashes"]),
            "task_edited_checkout_times": _json_object(row["times"]),
        }
        for task_id in variables["task_edited_checkout_hashes"]:
            stamps = task_checkout_edit_times(variables, task_id, root)
            for entry, digest in task_checkout_edit_hashes(variables, task_id, root).items():
                if entry not in wanted:
                    continue
                if entry not in current:
                    current[entry] = _current_digest(root, entry)
                if digest != current[entry]:
                    continue
                candidate = (stamps.get(entry, 0.0), task_id)
                if entry not in newest or candidate > newest[entry]:
                    newest[entry] = candidate
    return {entry: task_id for entry, (_, task_id) in newest.items()}


def content_recorded_paths(
    variables: Mapping[str, Any], checkout_root: str, paths: Iterable[str]
) -> set[str]:
    """Return the paths, among ``paths``, whose current content matches a record in ``variables``."""
    root = normalize_task_checkout_root(checkout_root)
    hashes = variables.get("task_edited_checkout_hashes")
    if root is None or not isinstance(hashes, dict):
        return set()
    records: dict[str, set[str | None]] = {}
    for task_id in hashes:
        for entry, digest in task_checkout_edit_hashes(
            {"task_edited_checkout_hashes": hashes}, task_id, root
        ).items():
            records.setdefault(entry, set()).add(digest)
    return {
        path for path in paths if path in records and _current_digest(root, path) in records[path]
    }


def _current_digest(root: str, path: str) -> str | None:
    try:
        return file_fingerprint(Path(root) / path)
    except OSError:
        return _UNREADABLE


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
            fingerprints[rel] = file_fingerprint(root / rel)
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
            if file_fingerprint(Path(root_key) / rel) == expected:
                approved.add(rel)
        except OSError:
            continue
    return approved


def _checkout_relative(root: Path, path: str) -> str:
    joined = Path(os.path.normpath(root / path))
    try:
        rel = (joined.parent.resolve(strict=False) / joined.name).relative_to(root)
    except ValueError:
        raise ValueError(f"{path!r} is outside checkout {root}.") from None
    if not path.strip() or rel == Path("."):
        raise ValueError(f"{path!r} does not name a path inside checkout {root}.")
    return rel.as_posix()
