"""Ownership conflict scope and actionable diagnostics, independent of I/O."""

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, replace

from gobby.workflows.task_claim_state import task_edit_path_covers


@dataclass(frozen=True)
class ForeignPathOwner:
    """Active foreign task/session attribution for one repo path."""

    path: str
    session_ref: str
    task_ref: str
    owner_session_id: str
    owner_task_id: str | None


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
        "Commit blocked: staged path(s) hold another live session's uncommitted work "
        "(rule 2: do not commit files you did not create or mutate):",
        *[
            f"- {owner.path} — session {owner.session_ref}, "
            + (f"task {owner.task_ref}" if owner.owner_task_id else "no claimed task")
            for owner in ordered_conflicts
        ],
        "Commit only your own paths with `git commit --only -- <paths>`, or ask each "
        "owner with `gobby-agents.send_message` to commit first.",
    ]
    return "\n".join(lines)


def format_unverified_dirty_edit_reason(paths: Set[str]) -> str:
    listed = ", ".join(sorted(paths))
    return (
        f"Edit blocked: Gobby could not verify whether {listed} carries another "
        "session's uncommitted work (git status unavailable). Retry once the "
        "repository is responsive."
    )


def format_dirty_edit_reason(
    conflicts: set[ForeignPathOwner],
    unowned: Set[str] = frozenset(),
    approval_commands: Sequence[str] = (),
) -> str:
    ordered_conflicts = sorted(
        conflicts, key=lambda item: (item.path, item.session_ref, item.task_ref)
    )
    return "\n".join(
        [
            "Edit blocked: dirty path(s) were changed outside this session "
            "(rule 3: you may not mutate files that are dirty):",
            *[
                f"- {owner.path} — session {owner.session_ref}, "
                + (f"task {owner.task_ref}" if owner.owner_task_id else "no claimed task")
                for owner in ordered_conflicts
            ],
            *[f"- {path} — no live session owns it" for path in sorted(unowned)],
            *(
                ["Ask each owner with `gobby-agents.send_message` to commit its work."]
                if conflicts
                else []
            ),
            *(
                [
                    "Unowned paths need the operator's approval. Ask the user to run, in "
                    "their own shell:",
                    *[f"  {command}" for command in approval_commands],
                ]
                if unowned
                else []
            ),
            "Then retry.",
        ]
    )
