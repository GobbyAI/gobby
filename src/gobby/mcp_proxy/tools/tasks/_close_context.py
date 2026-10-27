"""Task edit ownership and step records shared by close evaluation and finalization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from gobby.mcp_proxy.tools._task_query_pagination import collect_task_query_pages
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._task_scope import collect_commit_paths_async
from gobby.storage.tasks import Task
from gobby.tasks.attributed_roots import is_ignorable_attributed_root
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.step_records import StepRequirements, task_step_requirements
from gobby.workflows.commit_guard import (
    DirtyEditOwnershipInspectionError,
    foreign_owned_dirty_paths_async,
)
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.task_claim_state import (
    task_edit_path_covers,
    task_edited_checkout_paths,
    task_edited_file_set,
    task_live_checkout_files,
)
from gobby.workflows.task_dirty_state import committable_task_paths_async

logger = logging.getLogger(__name__)


class CloseAttributionLookupError(Exception):
    """The owner's persisted variables could not be loaded for close evaluation."""


@dataclass(frozen=True)
class CloseAttributionSnapshot:
    clean_checkouts: dict[str, frozenset[str]]
    complete: bool


async def step_requirements(
    ctx: RegistryContext, task: Task, commit_shas: Sequence[str]
) -> StepRequirements:
    """Judge step records against the close's prospective commits, as final close will."""
    prospective = replace(task, commits=list(commit_shas))
    return await asyncio.to_thread(task_step_requirements, ctx.task_manager.db, prospective)


def children(ctx: RegistryContext, task_id: str) -> list[Task]:
    return collect_task_query_pages(ctx.task_manager.list_tasks, parent_task_id=task_id)


async def capture_attribution(
    ctx: RegistryContext,
    *,
    task: Task,
    resolved_session_id: str,
    repo_path: str,
    prospective_commit_shas: Sequence[str],
    machine_id: str | None = None,
) -> CloseAttributionSnapshot:
    owner = get_claimed_session_id(task) or resolved_session_id
    try:
        variables = await asyncio.to_thread(ctx.session_var_manager.get_variables, owner)
    except (KeyError, ValueError, TypeError) as exc:
        raise CloseAttributionLookupError(
            "Could not load the task owner's session variables."
        ) from exc
    raw = task_edited_file_set(variables, task.id)
    checkouts = task_live_checkout_files(variables, task.id)
    recorded_paths = {path for _, path in task_edited_checkout_paths(variables, task.id)}
    complete = raw <= recorded_paths
    if not raw and not checkouts and prospective_commit_shas:
        try:
            checkouts[repo_path] = set(
                await collect_commit_paths_async(list(prospective_commit_shas), repo_path)
            )
        except RuntimeError:
            # The ancestry invariant reports unresolved linked commits.
            logger.debug("Cannot inspect linked paths for %s", task.id, exc_info=True)
    clean_checkouts: dict[str, frozenset[str]] = {}
    for checkout_root, checkout_paths in checkouts.items():
        if await asyncio.to_thread(
            is_ignorable_attributed_root,
            ctx.task_manager.db,
            project_id=task.project_id,
            machine_id=machine_id,
            root=checkout_root,
        ):
            logger.debug(
                "Ignoring unregistered non-checkout root %s for %s", checkout_root, task.id
            )
            continue
        paths = frozenset(await committable_task_paths_async(checkout_paths, checkout_root))
        dirty = session_dirty_file_set_for_checkout(variables, checkout_root)
        committed = frozenset(
            path
            for path in paths
            if not any(
                task_edit_path_covers(path, item) or task_edit_path_covers(item, path)
                for item in dirty
            )
        )
        if committed:
            try:
                foreign = await foreign_owned_dirty_paths_async(
                    ctx.task_manager.db,
                    session_id=owner,
                    project_id=task.project_id,
                    checkout_root=checkout_root,
                    paths=committed,
                    release_clean=False,
                )
            except DirtyEditOwnershipInspectionError:
                complete = False
                logger.warning("Cannot inspect foreign ownership during close", exc_info=True)
            else:
                paths -= foreign
        clean_checkouts[checkout_root] = paths
    return CloseAttributionSnapshot(clean_checkouts, complete)
