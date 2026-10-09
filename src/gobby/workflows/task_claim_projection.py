"""Persist canonical claim projections with task-row-first lock ordering."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.claimed_task_extra_skills import refresh_claimed_task_extra_skills
from gobby.workflows.task_claim_state import active_task_id_for_edit

if TYPE_CHECKING:
    from gobby.workflows.state_manager import SessionVariableManager


def _claim_ids(variables: dict[str, Any]) -> set[str]:
    """Ignore malformed projection keys rather than binding them as native UUIDs."""
    claims = variables.get("claimed_tasks")
    result: set[str] = set()
    if isinstance(claims, dict):
        for task_id in claims:
            try:
                result.add(str(UUID(str(task_id))))
            except ValueError:
                continue
    return result


def merge_claimed_task_projection(
    manager: SessionVariableManager,
    session_id: str,
    updates: dict[str, Any],
    observed_claim_task_id: str | None,
    inherited_active_task_id: str | None = None,
) -> bool:
    """Merge a snapshot without restoring lost claims or replacing newer claims.

    Lock canonical task rows in UUID order before the session-variable advisory
    lock. A newly persisted claim can arrive between those locks. In that case,
    release the transaction and retry with its row included; never acquire a
    task row while holding the variable lock.
    """
    task_ids = _claim_ids(updates) | _claim_ids(
        manager.get_variable_subset(session_id, ("claimed_tasks",))
    )
    if observed_claim_task_id is not None:
        task_ids.add(observed_claim_task_id)
    tasks = LocalTaskManager(manager.db)

    while True:
        with manager.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, claimed_by_session_id, closed_at, seq_num FROM tasks "
                "WHERE id = ANY(%s::uuid[]) OR claimed_by_session_id = %s "
                "ORDER BY id FOR UPDATE",
                (sorted(task_ids), session_id),
            ).fetchall()
            covered_ids = task_ids | {str(row["id"]) for row in rows}
            owned = {
                str(row["id"]): f"#{row['seq_num']}" if row["seq_num"] else str(row["id"])[:8]
                for row in rows
                if str(row["claimed_by_session_id"]) == session_id and row["closed_at"] is None
            }

            def mutate(
                variables: dict[str, Any],
                *,
                covered_ids: set[str] = covered_ids,
                owned: dict[str, str] = owned,
            ) -> tuple[bool, bool]:
                nonlocal task_ids
                current_ids = _claim_ids(variables)
                if not current_ids <= covered_ids:
                    task_ids |= current_ids
                    return False, False

                current_active = variables.get("active_task_id")
                if (
                    not current_ids
                    and "task_selection_history" not in variables
                    and inherited_active_task_id in owned
                ):
                    current_active = inherited_active_task_id
                variables.update(updates)
                variables["claimed_tasks"] = owned
                variables["task_claimed"] = bool(owned)
                # Storage selects at commit time. A delayed provider result or
                # reconcile snapshot never reselects its older claim.
                variables["active_task_id"] = current_active
                variables["active_task_id"] = active_task_id_for_edit(variables)
                # With no claim state to say which claim was active, a sole claim is.
                if not current_ids and len(owned) == 1 and variables["active_task_id"] is None:
                    variables["active_task_id"] = next(iter(owned))
                if not owned:
                    variables["task_has_commits"] = False
                refresh_claimed_task_extra_skills(variables, tasks)
                return True, True

            if manager._mutate_variables(
                session_id,
                mutate,
                keys=(
                    *updates,
                    "claimed_tasks",
                    "task_claimed",
                    "task_has_commits",
                    "enforce_tdd",
                    "claimed_task_extra_skills",
                    "unresolvable_claimed_task_extra_skills",
                    "claimed_task_requires_tdd",
                    "claimed_task_acceptance_test_paths",
                    "claimed_task_is_source_work",
                ),
            ):
                return True
