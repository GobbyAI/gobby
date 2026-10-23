"""Project-path resolution for workflow hook rules (#24524)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.hooks.checkout_reads import event_machine_id, node_origin
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import GLOBAL_PROJECT_ID, ORPHANED_PROJECT_ID, PERSONAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.workflows.engine import RuleEngine

logger = logging.getLogger(__name__)

_NO_REPO_PROJECT_CONSTANTS = frozenset(
    {
        PERSONAL_PROJECT_ID,
        GLOBAL_PROJECT_ID,
        ORPHANED_PROJECT_ID,
    }
)
_NO_REPO_LEGACY_PROJECT_IDS = frozenset({"_personal", "_global", "_orphaned"})
_NO_REPO_SYSTEM_PROJECTS: frozenset[str] = frozenset(
    {
        *_NO_REPO_PROJECT_CONSTANTS,
        *_NO_REPO_LEGACY_PROJECT_IDS,
        "_migrated",
    }
)


def _is_known_no_repo_project(project_id: str | None) -> bool:
    return isinstance(project_id, str) and project_id in _NO_REPO_SYSTEM_PROJECTS


def resolve_hook_project_path(
    rule_engine: RuleEngine | None, event: HookEvent, worktree_root: str | None
) -> str | None:
    """Resolve the best available filesystem path for workflow git checks."""
    from gobby.storage.project_checkouts import (
        CheckoutNotFoundError,
        CheckoutSentinelRejectedError,
        MissingMachineContextError,
        OverlayRegistrationRejectedError,
        require_root,
        resolve_operation_root,
    )
    from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
    from gobby.storage.workspace_machine_scope import (
        MachineOwnershipMismatchError,
        require_local_machine_id,
    )

    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    if metadata is not event.metadata:
        event.metadata = metadata
    if worktree_root:
        metadata["project_path"] = worktree_root
    project_id = event.project_id
    if (
        not project_id
        or rule_engine is None
        or _is_known_no_repo_project(project_id)
        or project_id in CHECKOUT_FREE_PROJECT_IDS
    ):
        return worktree_root

    if node_origin():
        # A node's own checkout rows name its roots; the hub never resolves a node path.
        row = LocalProjectCheckoutManager(rule_engine.db).get(event_machine_id() or "", project_id)
        if row is not None and (
            not worktree_root or os.path.normpath(worktree_root) == os.path.normpath(row.root_path)
        ):
            metadata["project_path"] = row.root_path
            return row.root_path
        return worktree_root

    # Checkout lookups fail soft on the hook hot path: a missing row, an
    # unregistered root, or a machine-context gap degrades to the git
    # worktree root (or None) so rule evaluation still runs.
    soft_errors = (
        CheckoutNotFoundError,
        CheckoutSentinelRejectedError,
        MissingMachineContextError,
        OverlayRegistrationRejectedError,
        MachineOwnershipMismatchError,
    )
    db = rule_engine.db
    try:
        machine_id = require_local_machine_id(
            None, resource_kind="project_checkout", resource_id=project_id
        )
        primary = require_root(db, project_id, machine_id)
    except soft_errors as exc:
        logger.debug("No checkout for project %s; using %r: %s", project_id, worktree_root, exc)
        return worktree_root
    if not worktree_root or Path(worktree_root).resolve() == Path(primary).resolve():
        metadata["project_path"] = primary
        return primary
    try:
        resolved = resolve_operation_root(db, project_id, machine_id, overlay_path=worktree_root)
    except soft_errors as exc:
        logger.debug("Unregistered root %s for project %s: %s", worktree_root, project_id, exc)
        return worktree_root
    metadata["project_path"] = resolved
    return resolved
