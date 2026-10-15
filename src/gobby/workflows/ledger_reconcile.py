"""Reconcile the session edit ledgers against git, bounded to the ledger's own paths.

The daemon records every path a session edits (``session_dirty_files`` and the
per-task ``task_edited_files`` ledgers). Those ledgers answer "did this session
dirty files" without touching git. Git is consulted only here, after the
session's own git activity, and only for the ledger's paths: a pathspec-limited
``git status`` refreshes the matching index entries and never walks the tree.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from gobby.workflows.observer_commits import commit_link_succeeded
from gobby.workflows.observer_utils import _extract_shell_command, _shell_tool_succeeded
from gobby.workflows.task_claim_state import (
    normalize_task_checkout_root,
    normalize_task_edited_path,
    task_edit_path_covers,
)
from gobby.workflows.task_dirty_state import task_dirty_paths_async

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.workflows.state_manager import SessionVariableManager

logger = logging.getLogger(__name__)

SESSION_DIRTY_FILES_VARIABLE = "session_dirty_files"
SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE = "session_dirty_file_checkouts"

# ``git`` at the start of the command or after a shell separator. Any git
# invocation may have changed the tree or the index, so all of them reconcile.
_GIT_INVOCATION_RE = re.compile(r"(?:^|[\s;&|(`])git\b")
# ``git stash`` hides dirt that ``git stash pop`` brings back without any edit
# event, so neither may release ledger paths; the ledger stays dirty instead.
_GIT_STASH_RE = re.compile(r"(?:^|[\s;&|(`])git\s+stash\b")


def session_dirty_file_set(variables: Mapping[str, Any]) -> set[str]:
    """Return the paths this session edited and has not reconciled as clean."""
    raw = variables.get(SESSION_DIRTY_FILES_VARIABLE)
    values = raw if isinstance(raw, list) else []
    return {path for value in values if (path := normalize_task_edited_path(value)) is not None}


def git_activity_detected(event: HookEvent) -> bool:
    """Return whether this after_tool event ran git or linked a commit."""
    command = _extract_shell_command(event)
    if command and _GIT_INVOCATION_RE.search(command):
        if _GIT_STASH_RE.search(command):
            return False
        return _shell_tool_succeeded(event) is not False
    return commit_link_succeeded(event)


def session_dirty_file_set_for_checkout(
    variables: Mapping[str, Any], project_path: str
) -> set[str]:
    """Return session-dirty paths whose edits ``project_path``'s git can verify.

    A path recorded only in another checkout is invisible to this git and is
    left out; a path with no recorded checkout is offered as before.
    """
    dirty = session_dirty_file_set(variables)
    raw_checkouts = variables.get(SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE)
    if not isinstance(raw_checkouts, dict) or not raw_checkouts:
        return dirty
    root = normalize_task_checkout_root(project_path)
    recorded: set[str] = set()
    visible: set[str] = set()
    for checkout, paths in raw_checkouts.items():
        if not isinstance(paths, list):
            continue
        normalized = {
            path for value in paths if (path := normalize_task_edited_path(value)) is not None
        }
        recorded |= normalized
        if str(checkout) == root:
            visible |= normalized
    return {path for path in dirty if path in visible or path not in recorded}


async def reconcile_edit_ledgers(
    variables: dict[str, Any],
    session_id: str,
    *,
    variable_manager: SessionVariableManager,
    project_path: str,
) -> list[str]:
    """Release session dirty paths that git reports clean in ``project_path``.

    Runs one status over the ledger paths only. A failed or timed-out status
    leaves every ledger unchanged, so the session stays dirty until a later
    reconcile succeeds; nothing here raises. Task attribution is close
    evidence: a clean path, even one another task committed, stays attributed.
    """
    candidates = session_dirty_file_set_for_checkout(variables, project_path)
    if not candidates:
        return []

    dirty = await task_dirty_paths_async(candidates, project_path)
    if dirty is None:
        logger.warning(
            "Session %s: edit ledger reconcile skipped; git status unavailable for "
            "%d path(s) in %s",
            session_id,
            len(candidates),
            project_path,
        )
        return []
    normalized_dirty = {
        path for value in dirty if (path := normalize_task_edited_path(value)) is not None
    }
    clean = {
        candidate
        for candidate in candidates
        if not any(task_edit_path_covers(candidate, path) for path in normalized_dirty)
    }
    if not clean:
        return []

    released = sorted(clean)
    await asyncio.to_thread(
        variable_manager.release_session_dirty_files,
        session_id,
        released,
        checkout_root=project_path,
    )

    refreshed = await asyncio.to_thread(variable_manager.get_variables, session_id)
    variables[SESSION_DIRTY_FILES_VARIABLE] = refreshed.get(SESSION_DIRTY_FILES_VARIABLE, [])
    variables[SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE] = refreshed.get(
        SESSION_DIRTY_FILE_CHECKOUTS_VARIABLE, {}
    )
    return released
