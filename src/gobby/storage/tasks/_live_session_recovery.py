"""Recovery of live-session task claims whose owning sessions expired."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import (
    LIVE_SESSION_STATUSES,
    is_contestable_terminal_expiry,
)
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._transitions import (
    escalate_task_if_owned,
    release_task_claim_if_owned,
)
from gobby.workflows.git_utils import resolve_git_worktree_root
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import remove_claimed_task, task_edited_file_set
from gobby.workflows.task_dirty_state import task_dirty_paths

logger = logging.getLogger(__name__)

_LIVE_SESSION_LABEL = "live-session"
_LIVE_OWNER_STATUSES = LIVE_SESSION_STATUSES


@dataclass(frozen=True)
class LiveSessionRecoveryResult:
    released: int = 0
    escalated: int = 0
    raced: int = 0


def recover_expired_live_session_claims(
    db: HubDatabase,
    *,
    project_id: str | None = None,
) -> LiveSessionRecoveryResult:
    """Release clean expired claims and escalate dirty or indeterminate ones."""
    task_manager = LocalTaskManager(db)
    session_manager = SessionManager(db)
    variable_manager = SessionVariableManager(db)
    project_manager = LocalProjectManager(db)
    tasks = task_manager.list_tasks(
        project_id=project_id,
        claimed=True,
        closed=False,
        escalated=False,
        label=_LIVE_SESSION_LABEL,
        limit=0,
    )

    released = 0
    escalated = 0
    raced = 0
    for task in tasks:
        owner = task.claimed_by_session_id
        if not owner:
            continue
        session_lookup_failed = False
        try:
            session = session_manager.get(owner)
        except Exception:
            logger.warning(
                "Could not load owning session %s for live-session task %s",
                owner,
                task.id,
                exc_info=True,
            )
            session = None
            session_lookup_failed = True
        if session is not None and (
            session.status in _LIVE_OWNER_STATUSES or is_contestable_terminal_expiry(session)
        ):
            # Recovery costs more here than a claim release does: a dirty task is
            # escalated and _clear_claim_variables pops the attribution #20789
            # keeps across every other claim release. Revival restores neither, so
            # an expiry that pane ownership can still reverse is not a dead owner.
            continue

        try:
            attributed_paths = _load_attributed_paths(db, variable_manager, owner, task.id)
        except Exception:
            logger.warning(
                "Could not load attributed files for expired live-session task %s",
                task.id,
                exc_info=True,
            )
            attributed_paths = None

        dirty_paths: set[str] | None
        if session_lookup_failed:
            dirty_paths = None
        elif attributed_paths == set():
            dirty_paths = set()
        elif attributed_paths is None:
            dirty_paths = None
        else:
            workspace = _resolve_workspace(session, project_manager, task.project_id)
            dirty_paths = (
                task_dirty_paths(attributed_paths, workspace) if workspace is not None else None
            )

        if dirty_paths == set():
            transitioned = release_task_claim_if_owned(db, task.id, expected_owner=owner)
            if transitioned is None:
                raced += 1
                continue
            released += 1
        else:
            evidence_paths = dirty_paths if dirty_paths is not None else attributed_paths
            transitioned = escalate_task_if_owned(
                db,
                task.id,
                reason=_escalation_reason(session, owner, evidence_paths, dirty_paths is None),
                expected_owner=owner,
            )
            if transitioned is None:
                raced += 1
                continue
            escalated += 1
        _clear_claim_variables(db, variable_manager, owner, task.id)

    return LiveSessionRecoveryResult(released=released, escalated=escalated, raced=raced)


def _load_attributed_paths(
    db: HubDatabase,
    variable_manager: SessionVariableManager,
    owner: str,
    task_id: str,
) -> set[str] | None:
    if not _session_variables_exist(db, owner):
        return None
    return task_edited_file_set(variable_manager.get_variables(owner), task_id)


def _session_variables_exist(db: HubDatabase, session_id: str) -> bool:
    return (
        db.fetchone(
            "SELECT session_id FROM session_variables WHERE session_id = %s",
            (session_id,),
        )
        is not None
    )


def _resolve_workspace(
    session: object | None,
    project_manager: LocalProjectManager,
    project_id: str,
) -> str | None:
    session_cwd: str | None = None
    terminal_context = getattr(session, "terminal_context", None)
    if isinstance(terminal_context, dict):
        raw_cwd = terminal_context.get("cwd")
        if isinstance(raw_cwd, str):
            session_cwd = raw_cwd
    project = project_manager.get(project_id)
    project_path = project.repo_path if project is not None else None
    return resolve_git_worktree_root(session_cwd, project_path)


def _escalation_reason(
    session: object | None,
    owner: str,
    paths: set[str] | None,
    indeterminate: bool,
) -> str:
    seq_num = getattr(session, "seq_num", None)
    session_ref = f"#{seq_num}" if seq_num else owner
    path_text = ", ".join(sorted(paths or ())) or "(unavailable)"
    state = "indeterminate dirty state" if indeterminate else "uncommitted task-attributed changes"
    return (
        f"Expired live-session owner {session_ref} has {state}. Task-attributed paths: {path_text}"
    )


def _clear_claim_variables(
    db: HubDatabase,
    variable_manager: SessionVariableManager,
    owner: str,
    task_id: str,
) -> None:
    try:
        if not _session_variables_exist(db, owner):
            return
        variables = variable_manager.get_variables(owner)
        variable_manager.merge_variables(owner, remove_claimed_task(variables, task_id))
    except Exception:
        logger.debug(
            "Best-effort expired live-session variable cleanup failed for %s",
            owner,
            exc_info=True,
        )
