"""Ownership conflict scope and actionable diagnostics, independent of I/O."""

import json
from collections.abc import Mapping, Set
from dataclasses import dataclass, replace

from gobby.workflows.task_claim_state import task_edit_path_covers


@dataclass(frozen=True)
class ForeignPathOwner:
    """Active foreign task/session attribution for one repo path."""

    path: str
    session_ref: str
    task_ref: str
    owner_session_id: str
    owner_task_id: str


def scope_foreign_owners(
    owners: Mapping[str, tuple[ForeignPathOwner, ...]],
    requested: Set[str],
    current_owned: Set[str],
) -> dict[str, tuple[ForeignPathOwner, ...]]:
    """Inspect requested children without inheriting unrelated ancestor dirt.

    A request for the ledger entry itself or one of its ancestors still owns
    the whole entry's scope. Only strict children can prefer requester ownership.
    """
    scoped: dict[str, set[ForeignPathOwner]] = {}
    for owned, path_owners in owners.items():
        for path in requested:
            if owned != path and task_edit_path_covers(owned, path):
                if any(task_edit_path_covers(own, path) for own in current_owned):
                    continue
                scope = path
            elif task_edit_path_covers(path, owned):
                scope = owned
            else:
                continue
            scoped.setdefault(scope, set()).update(
                replace(owner, path=scope) for owner in path_owners
            )
    return {
        path: tuple(sorted(path_owners, key=lambda owner: (owner.session_ref, owner.task_ref)))
        for path, path_owners in scoped.items()
    }


def format_ref(seq_num: object, fallback_id: str) -> str:
    return f"#{seq_num}" if isinstance(seq_num, int) else fallback_id


def format_session_ref(
    seq_num: object, session_id: str, *, project_name: object, project_id: object
) -> str:
    """Project-qualified session reference, matching ``Session.ref``."""
    if not isinstance(seq_num, int):
        return session_id
    name = project_name.strip() if isinstance(project_name, str) else ""
    project = name or (str(project_id) if project_id else "")
    return f"{project}#{seq_num}"


def format_conflict_reason(conflicts: set[ForeignPathOwner]) -> str:
    ordered_conflicts = sorted(
        conflicts, key=lambda item: (item.path, item.session_ref, item.task_ref)
    )
    lines = [
        "Commit blocked: staged path(s) belong to another active task/session:",
        *[
            f"- {owner.path} — session {owner.session_ref}, task {owner.task_ref}"
            for owner in ordered_conflicts
        ],
        "Ask each owner with `gobby-agents.send_message` to verify its work is committed, "
        "then run its matching release call:",
        *[
            f"- session {owner.session_ref}: "
            f"`gobby-tasks.release_task_paths(task_id={json.dumps(owner.task_ref)}, "
            f"paths=[{json.dumps(owner.path)}])`"
            for owner in ordered_conflicts
        ],
        "Commit only your paths with `git commit --only -- <owned paths>`; "
        "foreign staged entries will remain intact.",
    ]
    return "\n".join(lines)


def format_unverified_dirty_edit_reason(paths: Set[str]) -> str:
    listed = ", ".join(sorted(paths))
    return (
        f"Edit blocked: Gobby could not verify whether {listed} carries another "
        "session's uncommitted work (git status unavailable). Retry once the "
        "repository is responsive."
    )


def format_dirty_edit_reason(conflicts: set[ForeignPathOwner]) -> str:
    ordered_conflicts = sorted(
        conflicts, key=lambda item: (item.path, item.session_ref, item.task_ref)
    )
    return "\n".join(
        [
            "Edit blocked: dirty path(s) belong to another active task/session:",
            *[
                f"- {owner.path} — session {owner.session_ref}, task {owner.task_ref}"
                for owner in ordered_conflicts
            ],
            "Ask each owner with `gobby-agents.send_message` to make a buildable WIP "
            "commit before you continue.",
            "For work that cannot be committed, migrate it through `gobby-worktrees`.",
            "For a stale owner, reclaim its task with:",
            *[
                f"- `gobby-tasks.claim_task(task_id={json.dumps(task_ref)}, force=true)`"
                for task_ref in dict.fromkeys(owner.task_ref for owner in ordered_conflicts)
            ],
        ]
    )
