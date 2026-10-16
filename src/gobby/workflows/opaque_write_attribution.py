"""Find the writes of a program whose body the classifier could not read.

``uv run python tool.py`` names no write paths before it runs, so after-tool
attribution has nothing to record and its outputs would be dirty paths no
session owns, which rule 3 then refuses to the session that wrote them. These
are the one deliberate whole-checkout statuses (the edit ledgers otherwise stay
pathspec-bounded): one as such a program starts and one after it ends. Only
paths that went from clean to dirty in between, and that no other live session
holds, are the program's. Dirt that predates the call stays unowned, so rule 3
still guards it.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from gobby.utils.daemon_git import GitOk, daemon_git, parse_porcelain_v1_z
from gobby.workflows.commit_guard import _live_path_owners
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.task_claim_state import overlapping_task_edit_paths, task_edit_path_covers

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


async def checkout_dirty_paths(checkout_root: str) -> set[str] | None:
    """Every modified, deleted or untracked path in the checkout; None when Git fails."""
    result = await daemon_git.status(checkout_root, timeout=10.0)
    if not isinstance(result, GitOk):
        return None
    try:
        entries = parse_porcelain_v1_z(result.stdout)
    except ValueError:
        return None
    return {
        path for entry in entries for path in (entry.path, entry.original_path) if path is not None
    }


async def opaque_program_writes(
    db: HubDatabase,
    variables: dict[str, Any],
    *,
    session_id: str,
    checkout_root: str,
    baseline: set[str],
) -> list[str]:
    """Return absolute paths dirtied since ``baseline`` that no session holds."""
    dirty = await checkout_dirty_paths(checkout_root)
    if dirty is None:
        return []
    own = session_dirty_file_set_for_checkout(variables, checkout_root)
    changed = {
        path
        for path in dirty - baseline
        if not any(task_edit_path_covers(owned, path) for owned in own)
    }
    if not changed:
        return []
    owners = await asyncio.to_thread(
        _live_path_owners,
        db,
        checkout_root=checkout_root,
        paths=changed,
        exclude_session_id=session_id,
    )
    return sorted(
        os.path.join(checkout_root, path)
        for path in changed
        if not overlapping_task_edit_paths(owners, [path])
    )
