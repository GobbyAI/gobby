"""Find a session's handed-off claims, which no longer hold its claim capacity (#23665).

A handed-off claim waits only on review, landing or close. A reviewer recorded an
``independent_review_approval`` receipt for its committed candidate, and no path
attributed to it is uncommitted. It keeps its owner and edit history while a new
claim becomes the session's active one. Stage ``submit_for_review`` already
releases the claim, so the receipt is the only attested hand-off for an open claim.
"""

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_paths import (
    _claimed_session_worktree_path,
    _lifecycle_checkout_root,
)
from gobby.tasks.close_receipts import INDEPENDENT_REVIEW_APPROVAL, list_close_receipts
from gobby.workflows.task_claim_state import task_edited_file_set, task_live_checkout_files
from gobby.workflows.task_dirty_state import has_committable_edits


def handed_off_claim_ids(
    ctx: RegistryContext,
    session_id: str,
    project_id: str,
    *,
    target_task_id: str | None = None,
) -> frozenset[str]:
    """Return the session's other open claims that no longer block a new claim.

    Fails closed: a claim whose attributed paths git cannot verify still blocks.
    """
    reviewed = [
        task.id
        for task in ctx.task_manager.list_tasks(claimed_by_session_id=session_id, closed=False)
        if task.id != target_task_id
        and any(
            receipt.kind == INDEPENDENT_REVIEW_APPROVAL
            for receipt in list_close_receipts(ctx.task_manager.db, task.id)
        )
    ]
    if not reviewed:
        return frozenset()
    try:
        variables = ctx.session_var_manager.get_variables(session_id)
    except KeyError:
        variables = {}

    handed_off: set[str] = set()
    for task_id in reviewed:
        # Checkout-scoped attribution names the checkout that recorded each edit, as
        # ledger reconciliation reads it. A flat-only ledger is verified in the
        # claimant's checkout.
        paths_by_root = task_live_checkout_files(variables, task_id)
        flat_paths = task_edited_file_set(variables, task_id)
        if not paths_by_root and flat_paths:
            root = _claimant_checkout_root(ctx, session_id=session_id, project_id=project_id)
            if root is None:
                continue
            paths_by_root = {root: flat_paths}
        if not any(has_committable_edits(paths, root) for root, paths in paths_by_root.items()):
            handed_off.add(task_id)
    return frozenset(handed_off)


def _claimant_checkout_root(
    ctx: RegistryContext,
    *,
    session_id: str,
    project_id: str,
) -> str | None:
    try:
        return _lifecycle_checkout_root(
            ctx,
            session_id=session_id,
            project_id=project_id,
            overlay_path=_claimed_session_worktree_path(
                ctx,
                session_id=session_id,
                project_id=project_id,
            ),
        )
    except ValueError:
        return None
