"""Checkout roots for plan operations, including registered worktrees and clones."""

from __future__ import annotations

import os
from pathlib import Path

from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    CheckoutNotFoundError,
    OverlayRegistrationRejectedError,
    resolve_operation_root,
)
from gobby.storage.sessions import SessionManager
from gobby.storage.workspace_machine_scope import require_local_machine_id
from gobby.utils.session_context import get_current_session_id


def resolve_plan_overlay_root(
    db: HubDatabase,
    project_id: str,
    plan_path: str | Path,
) -> Path | None:
    """Return the registered worktree or clone root that owns `plan_path`, if any.

    An absolute path belongs to the innermost registered overlay containing it. A
    relative path belongs to the overlay containing the caller session's workspace.
    """
    candidate = Path(plan_path)
    if candidate.is_absolute():
        start: Path | None = Path(os.path.abspath(candidate)).parent
    else:
        start = _caller_workspace(db)
    if start is None:
        return None
    machine_id = require_local_machine_id(
        None, resource_kind="project_checkout", resource_id=project_id
    )
    for directory in (start, *start.parents):
        try:
            overlay = resolve_operation_root(
                db, project_id, machine_id, overlay_path=str(directory)
            )
        except OverlayRegistrationRejectedError:
            continue
        return Path(overlay)
    return None


def resolve_plan_root(db: HubDatabase, project_id: str, plan_path: str | Path) -> Path:
    """Return the resolved root for `plan_path`: its registered overlay, else the checkout.

    Paths outside every registered root keep the primary checkout, so
    `normalize_plan_path` still rejects them as escaping the project root.
    """
    overlay = resolve_plan_overlay_root(db, project_id, plan_path)
    if overlay is not None:
        return overlay.resolve(strict=True)
    try:
        machine_id = require_local_machine_id(
            None, resource_kind="project_checkout", resource_id=project_id
        )
        return Path(resolve_operation_root(db, project_id, machine_id)).resolve(strict=True)
    except CheckoutNotFoundError as exc:
        raise ReviewEvidenceError(
            "project_not_found",
            f"project has no local repository: {project_id}",
        ) from exc


def _caller_workspace(db: HubDatabase) -> Path | None:
    session_id = get_current_session_id()
    session = SessionManager(db).get(session_id) if session_id else None
    if session is None or not session.workspace_path:
        return None
    return Path(session.workspace_path)
