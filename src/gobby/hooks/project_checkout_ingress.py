"""Cwd-marker checkout registration for authenticated local hook ingress."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project


def register_cwd_marker_checkout(
    db: HubDatabase,
    project_context: dict[str, Any],
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Register or skip a cwd-marker checkout. Typed refusals propagate."""
    from gobby.storage.project_checkouts import (
        LocalProjectCheckoutManager,
        MissingMachineContextError,
        SoftDeletedProjectRejectedError,
    )
    from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS, LocalProjectManager
    from gobby.storage.workspace_machine_scope import (
        MachineOwnershipMismatchError,
        require_local_machine_id,
    )
    from gobby.utils.checkout_root import validate_checkout_root

    project_id = str(project_context["id"])
    if project_id in CHECKOUT_FREE_PROJECT_IDS:
        return

    cwd = project_context.get("project_path")
    if not isinstance(cwd, str) or not cwd:
        return

    manager = LocalProjectManager(db)
    project = manager.get(project_id)
    if project is not None and project.deleted_at is not None:
        raise SoftDeletedProjectRejectedError(
            f"project {project_id} is soft-deleted; hook ingress does not restore"
        )

    try:
        machine_id = require_local_machine_id(
            None, resource_kind="project_checkout", resource_id=project_id
        )
    except RuntimeError as exc:
        if isinstance(exc, MachineOwnershipMismatchError):
            raise
        raise MissingMachineContextError(str(exc)) from exc

    if manager._repo_path_write_is_blocked(cwd, machine_id=machine_id):
        if project is not None:
            _refresh_stale_marker(cwd, project, project_context, logger)
        return

    root = validate_checkout_root(
        db,
        project_id=project_id,
        machine_id=machine_id,
        candidate_path=cwd,
        expected_marker_id=project_id,
    )
    if project is None:
        manager.ensure_exists(project_id, str(project_context.get("name") or "unknown"))
        project = manager.get(project_id)
        if project is None:
            raise RuntimeError(f"Project {project_id} not found after ID-targeted upsert")
        if project.deleted_at is not None:
            raise SoftDeletedProjectRejectedError(
                f"project {project_id} is soft-deleted; hook ingress does not restore"
            )

    LocalProjectCheckoutManager(db).register(machine_id, project_id, root)
    _refresh_stale_marker(cwd, project, project_context, logger)


def _refresh_stale_marker(
    cwd: str,
    project: Project,
    project_context: dict[str, Any],
    logger: logging.Logger | None,
) -> None:
    if str(project_context.get("name") or "") == project.name:
        return
    from gobby.utils.checkout_root import MarkerMismatchError
    from gobby.utils.project_init import refresh_marker_expected_id

    try:
        refresh_marker_expected_id(Path(cwd), project.id, project.name)
    except MarkerMismatchError:
        if logger:
            logger.warning("Refused stale marker refresh at %s", cwd)
