"""Settle an ended owner's background calls once the jobs recorded for them are gone.

A graceful exit is the only end that stops the owner's background work. A call settles only
when the job tree recorded at its receipt is empty afterwards. A call with no recorded tree
stays foreign, and so does every call under any other end, until an operator releases it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping
from typing import Any

from gobby.hooks.dispatchers import run_coro_blocking
from gobby.hooks.hook_types import SessionEndReason
from gobby.sessions.owner_process_tree import JobTree, job_tree_of, stop_recorded_job_trees
from gobby.workflows.git_operation_ownership import OPERATIONS_VARIABLE
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.worktree_shell_attribution import settle_ended_owner_background_calls

CLAIMED_SHELL_SNAPSHOTS = "claimed_shell_snapshots"

# Exit-like ends stop the CLI process. A clear, compaction, resume, idle or unspecified end can
# leave it running with its background work, so none of those stops it here.
GRACEFUL_END_REASONS = frozenset(
    {SessionEndReason.EXIT, SessionEndReason.LOGOUT, SessionEndReason.PROMPT_INPUT_EXIT}
)


def ended_owner_output_paths(manager: SessionVariableManager, session_id: str) -> set[str]:
    """Output files of the session's calls that still wait on their owner."""
    return {entry["output_path"] for _, entry in _waiting_calls(manager, session_id)}


def ended_owner_job_trees(manager: SessionVariableManager, session_id: str) -> dict[str, JobTree]:
    """The recorded job tree of each waiting call, by call id; a call without one is absent."""
    trees: dict[str, JobTree] = {}
    for call_id, entry in _waiting_calls(manager, session_id):
        tree = job_tree_of(entry.get("job_tree"))
        if call_id is not None and tree is not None:
            trees[call_id] = tree
    return trees


def settle_graceful_owner_end(
    manager: SessionVariableManager,
    session_id: str,
    terminal_context: Mapping[str, Any] | None,
    *,
    loop: Any,
    logger: logging.Logger,
) -> bool:
    """Stop the recorded jobs, then settle each call whose jobs are gone; True when none is left.

    The stop touches only the jobs recorded at each call's receipt, so the owner's own identity
    is not needed here. A call whose receipt recorded no job tree is never settled by this path.
    """
    if not ended_owner_output_paths(manager, session_id):
        return False
    settled = stop_recorded_job_trees(terminal_context, ended_owner_job_trees(manager, session_id))
    if settled:
        run_coro_blocking(
            settle_ended_owner_calls(manager, session_id, settled),
            loop,
            logger,
            label="settle_ended_owner",
        )
    return not ended_owner_output_paths(manager, session_id)


async def settle_ended_owner_calls(
    manager: SessionVariableManager, session_id: str, call_ids: Collection[str]
) -> None:
    """Settle the given calls' claimed shell edits, background calls and pending Git operations."""
    variables = await asyncio.to_thread(
        manager.get_variable_subset, session_id, (CLAIMED_SHELL_SNAPSHOTS, OPERATIONS_VARIABLE)
    )
    snapshots = variables.get(CLAIMED_SHELL_SNAPSHOTS)
    if isinstance(snapshots, dict):
        for call_id, entry in snapshots.items():
            if (
                call_id in call_ids
                and isinstance(entry, dict)
                and isinstance(entry.get("output_path"), str)
            ):
                await asyncio.to_thread(manager.adopt_claimed_shell_edits, session_id, call_id)
    await settle_ended_owner_background_calls(manager, session_id, call_ids)
    for entry in _values(variables.get(OPERATIONS_VARIABLE)):
        if not isinstance(entry, dict) or entry.get("pending") is not True:
            continue
        call_id = entry.get("call_id")
        if (
            isinstance(call_id, str)
            and call_id in call_ids
            and isinstance(entry.get("output_path"), str)
        ):
            await manager.finish_git_operation_call(session_id, call_id)


def _waiting_calls(
    manager: SessionVariableManager, session_id: str
) -> list[tuple[str | None, dict[str, Any]]]:
    """Each call that still waits on its owner and holds an output file, with its call id."""
    variables = manager.get_variable_subset(
        session_id, (CLAIMED_SHELL_SNAPSHOTS, OPERATIONS_VARIABLE)
    )
    candidates: list[tuple[str | None, Any]] = list(
        manager.background_worktree_calls(session_id).items()
    )
    snapshots = variables.get(CLAIMED_SHELL_SNAPSHOTS)
    if isinstance(snapshots, dict):
        candidates.extend(snapshots.items())
    for entry in _values(variables.get(OPERATIONS_VARIABLE)):
        if isinstance(entry, dict) and entry.get("pending") is True:
            call_id = entry.get("call_id")
            candidates.append((call_id if isinstance(call_id, str) else None, entry))
    return [
        (call_id, entry)
        for call_id, entry in candidates
        if isinstance(entry, dict) and isinstance(entry.get("output_path"), str)
    ]


def _values(container: Any) -> list[Any]:
    return list(container.values()) if isinstance(container, dict) else []
