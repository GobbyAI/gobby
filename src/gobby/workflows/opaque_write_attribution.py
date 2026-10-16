"""Find the writes of a program whose body the classifier could not read.

``uv run python tool.py out.txt`` names no write paths before it runs, so after-tool
attribution has nothing to record and its outputs would be dirty paths no
session owns, which rule 3 then refuses to the session that wrote them. The
only writer evidence is the command itself: a path operand the program was
given. An operand that went from clean to dirty while the program ran, and that
no other live session holds, is the caller's, just as ``cp a out.txt`` records
``out.txt``. Every other path that changed meanwhile, including files under an
operand directory, may be another writer's, so it stays unowned and rule 3
still guards it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from gobby.workflows.commit_guard import _live_path_owners
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.task_claim_state import overlapping_task_edit_paths, task_edit_path_covers
from gobby.workflows.task_dirty_state import task_dirty_paths_async

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

# Checkout root -> {"operands": [...], "dirty": [...]}, as stored on the call's binding.
OperandBaseline = dict[str, dict[str, list[str]]]


async def operand_baseline(operands_by_checkout: Mapping[str, set[str]]) -> OperandBaseline:
    """Each checkout's operands and those already dirty as the program starts.

    A checkout whose status fails is left out, so none of its operands is adopted.
    """
    baseline: OperandBaseline = {}
    for root, operands in operands_by_checkout.items():
        dirty = await task_dirty_paths_async(operands, root)
        if dirty is not None:
            baseline[root] = {"operands": sorted(operands), "dirty": sorted(dirty & operands)}
    return baseline


async def opaque_program_writes(
    db: HubDatabase,
    variables: dict[str, Any],
    *,
    session_id: str,
    baseline: OperandBaseline,
) -> list[str]:
    """Return absolute operand paths dirtied since ``baseline`` that no session holds."""
    paths: list[str] = []
    for root, entry in baseline.items():
        operands = set(entry["operands"])
        dirty = await task_dirty_paths_async(operands, root)
        if dirty is None:
            continue
        own = session_dirty_file_set_for_checkout(variables, root)
        changed = {
            path
            for path in (dirty & operands) - set(entry["dirty"])
            if not any(task_edit_path_covers(owned, path) for owned in own)
        }
        if not changed:
            continue
        owners = await asyncio.to_thread(
            _live_path_owners,
            db,
            checkout_root=root,
            paths=changed,
            exclude_session_id=session_id,
        )
        paths.extend(
            os.path.join(root, path)
            for path in sorted(changed)
            if not overlapping_task_edit_paths(owners, [path])
        )
    return paths
