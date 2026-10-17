"""Grounds that let a session write dirt its own ledger does not cover.

A task's carriers follow its claim: a path any session's task edit ledger
attributes to a task this session holds (``tasks.claimed_by_session_id``) is
this session's own. An operator approval names exact paths in one checkout for
one session, and holds for each path only while it keeps the content it had
when approved. Only the operator route writes the reserved approval variable: the
daemon's dirty-path-approvals endpoint, which admits only a verified operator API key.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    normalize_task_checkout_root,
    task_edited_file_set_for_checkout,
)

OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE = "operator_dirty_path_approvals"


def claimed_task_carriers(
    db: HubDatabase, *, session_id: str, checkout_root: str
) -> tuple[frozenset[str], set[str]]:
    """Return the tasks this session holds and the paths any session edited for them."""
    rows = db.fetchall(
        """
        SELECT tasks.id::text AS task_id,
               (session_variables.variables -> 'task_edited_file_checkouts'
                   -> tasks.id::text)::text AS checkouts
        FROM tasks
        LEFT JOIN session_tasks ON session_tasks.task_id = tasks.id
        LEFT JOIN session_variables ON session_variables.session_id = session_tasks.session_id
        WHERE tasks.claimed_by_session_id = %s
        """,
        (session_id,),
    )
    carriers: set[str] = set()
    for row in rows:
        if row["checkouts"] is None:
            continue
        task_id = str(row["task_id"])
        ledger = {"task_edited_file_checkouts": {task_id: json.loads(row["checkouts"])}}
        carriers |= task_edited_file_set_for_checkout(ledger, task_id, checkout_root)
    return frozenset(str(row["task_id"]) for row in rows), carriers


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
