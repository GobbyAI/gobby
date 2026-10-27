"""Attributed checkout roots that cannot hold a task's uncommitted work."""

from __future__ import annotations

import os

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.checkout_root import canonical_checkout_root


def is_ignorable_attributed_root(
    db: HubDatabase, *, project_id: str, machine_id: str | None, root: str
) -> bool:
    """Return True for a root that is neither a git checkout nor registered to the project.

    A missing or plain-directory root that no project checkout or worktree record names
    cannot hold the task's uncommitted work, so it must not block close gate 3 or claim
    release. A registered root stays fail-closed even when it vanished, and an unregistered
    git checkout stays inspected by Git.
    """
    canonical = canonical_checkout_root(root)
    if os.path.isdir(canonical) and os.path.exists(os.path.join(canonical, ".git")):
        return False
    if machine_id is None:
        return False
    registered = _registered(
        db, project_id=project_id, machine_id=machine_id, root=root, canonical=canonical
    )
    return not registered


def _registered(
    db: HubDatabase,
    *,
    project_id: str,
    machine_id: str,
    root: str,
    canonical: str,
) -> bool:
    for checkout in LocalProjectCheckoutManager(db).list_for_machine(machine_id):
        if (
            checkout.project_id == project_id
            and canonical_checkout_root(checkout.root_path) == canonical
        ):
            return True
    worktrees = LocalWorktreeManager(db)
    for path in (root, canonical):
        worktree = worktrees.get_by_path(path)
        if worktree is not None and worktree.project_id == project_id:
            return True
        if worktrees.has_path_on_other_machine(path):
            return True
    return False
