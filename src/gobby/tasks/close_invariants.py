"""The four data invariants and the required step records for closing a task."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set

from gobby.tasks.close_checklist import CloseChecklist, CloseGateResult
from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git
from gobby.workflows.task_dirty_state import task_dirty_paths_async

NO_WORK_CLOSE_REASONS = frozenset({"moot", "superseded", "wont_do"})
CLOSE_GATE_ORDER = (
    (1, "children_closed"),
    (2, "linked_commits"),
    (3, "uncommitted_task_edits"),
    (4, "integration_ancestry"),
    (5, "step_records"),
)


async def evaluate_close_invariants(
    *,
    open_children: Sequence[str],
    commit_shas: Sequence[str],
    reason: str,
    attributed_checkouts: Mapping[str, Set[str]],
    integration_repo_path: str | None,
    integration_ref: str | None,
    required_steps: Sequence[str],
    missing_steps: Sequence[str],
    attribution_complete: bool = True,
) -> CloseChecklist:
    """Evaluate every gate without changing task, Git, or review state."""
    gates = await _data_invariants(
        open_children=open_children,
        commit_shas=commit_shas,
        reason=reason,
        attributed_checkouts=attributed_checkouts,
        integration_repo_path=integration_repo_path,
        integration_ref=integration_ref,
        attribution_complete=attribution_complete,
    )
    details = {"required_steps": list(required_steps), "missing_steps": list(missing_steps)}
    gates.append(
        CloseGateResult(
            5,
            "step_records",
            "skipped" if not required_steps else "failed" if missing_steps else "passed",
            "Project policy requires no step records."
            if not required_steps
            else "Record the approved steps project policy requires: " + ", ".join(missing_steps)
            if missing_steps
            else "Every required step record is approved.",
            details,
        )
    )
    return CloseChecklist(tuple(gates))


async def _data_invariants(
    *,
    open_children: Sequence[str],
    commit_shas: Sequence[str],
    reason: str,
    attributed_checkouts: Mapping[str, Set[str]],
    integration_repo_path: str | None,
    integration_ref: str | None,
    attribution_complete: bool,
) -> list[CloseGateResult]:
    gates = [
        CloseGateResult(
            1,
            "children_closed",
            "failed" if open_children else "passed",
            "Close every child task first." if open_children else "Every child is closed.",
            {"open_children": list(open_children)},
        )
    ]
    has_disposition = bool(commit_shas) or reason.casefold() in NO_WORK_CLOSE_REASONS
    gates.append(
        CloseGateResult(
            2,
            "linked_commits",
            "passed" if has_disposition else "failed",
            "A linked commit or no-work disposition exists."
            if has_disposition
            else "Link a commit or supply an explicit no-work close reason.",
            {"commit_shas": list(commit_shas)},
        )
    )
    dirty: dict[str, list[str]] = {}
    unavailable: list[str] = []
    for checkout, paths in attributed_checkouts.items():
        dirty_paths = await task_dirty_paths_async(set(paths), checkout)
        if dirty_paths is None:
            unavailable.append(checkout)
        elif dirty_paths:
            dirty[checkout] = sorted(dirty_paths)
    clean = attribution_complete and not dirty and not unavailable
    gates.append(
        CloseGateResult(
            3,
            "uncommitted_task_edits",
            "passed" if clean else "failed",
            "Task-attributed files are clean."
            if clean
            else "Git could not inspect all task-attributed files."
            if unavailable or not attribution_complete
            else "Commit task-attributed changes before closing.",
            {"dirty_checkouts": dirty, "unavailable_checkouts": unavailable},
        )
    )
    if not commit_shas:
        gates.append(
            CloseGateResult(4, "integration_ancestry", "skipped", "No linked work to check.")
        )
        return gates

    tip = (
        await resolve_commit(integration_repo_path, integration_ref)
        if integration_ref and integration_repo_path
        else None
    )
    landed_shas: list[str] = []
    unlanded: list[str] = []
    unresolved: list[str] = []
    for sha in commit_shas:
        resolved = (
            await resolve_commit(integration_repo_path, sha) if integration_repo_path else None
        )
        if resolved is None:
            unresolved.append(sha)
        elif tip is not None and integration_repo_path is not None:
            result = await daemon_git.run(
                ("merge-base", "--is-ancestor", resolved, tip), cwd=integration_repo_path
            )
            if isinstance(result, GitOk):
                landed_shas.append(sha)
            elif isinstance(result, GitFailed) and result.returncode == 1:
                unlanded.append(sha)
            else:
                # Git could not answer, so the commit is neither proven landed nor unlanded.
                unresolved.append(sha)
    landed = tip is not None and not unresolved and not unlanded
    if reason.casefold() in NO_WORK_CLOSE_REASONS:
        # A no-work close carries no linked work: landed work closes as done, and unlanded
        # candidates leave through the audited unlink before the close.
        passed = False
        message = (
            f"Landed commits cannot close as {reason}; close with reason done."
            if landed_shas
            else "Unlink linked commits with unlink_commit before a no-work close."
        )
    else:
        passed = landed
        message = (
            "Linked work is on the integration branch."
            if landed
            else "Linked work must resolve and be an ancestor of the integration tip."
        )
    gates.append(
        CloseGateResult(
            4,
            "integration_ancestry",
            "passed" if passed else "failed",
            message,
            {
                "integration_ref": integration_ref,
                "integration_tip": tip,
                "integration_repo_path": integration_repo_path,
                "unresolved_commits": unresolved,
                "unlanded_commits": unlanded,
                "landed_commits": landed_shas,
            },
        )
    )
    return gates


async def resolve_commit(repo_path: str, ref: str) -> str | None:
    result = await daemon_git.run(
        ("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"), cwd=repo_path
    )
    return result.stdout.strip() if isinstance(result, GitOk) else None
