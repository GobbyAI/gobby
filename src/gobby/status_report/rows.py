"""Claim provenance and task relationships for status-report rows."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from gobby.status_report.model import IdleHolder, WorktreeRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._queries import projected_task_state_sql
from gobby.storage.worktrees import Worktree
from gobby.workflows.step_instances import AgentStepInstanceManager


@dataclass(frozen=True)
class ClaimHolder:
    task_id: str
    task_seq: int
    task_title: str
    session_id: str
    agent: str | None
    status: str | None
    claimed_at: datetime
    last_activity: datetime | None


def claim_holders(db: HubDatabase, project_id: str) -> dict[str, ClaimHolder]:
    rows = db.fetchall(
        """
        SELECT DISTINCT ON (ci.task_id)
            ci.task_id, t.seq_num, t.title, ci.session_id, ci.claimed_at,
            s.status, s.last_activity, ar.agent_name
        FROM task_claim_intervals ci
        JOIN tasks t ON t.id = ci.task_id
        LEFT JOIN sessions s ON s.id = ci.session_id
        LEFT JOIN LATERAL (
            SELECT r.agent_name FROM agent_runs r
            WHERE r.id = s.agent_run_id OR r.child_session_id = s.id
                OR r.claimed_session_id = s.id
            ORDER BY (r.id = s.agent_run_id) DESC NULLS LAST, r.created_at DESC, r.id
            LIMIT 1
        ) ar ON true
        WHERE t.project_id = %s AND t.closed_at IS NULL AND ci.released_at IS NULL
        ORDER BY ci.task_id, ci.claimed_at DESC, ci.session_id
        """,
        (project_id,),
    )
    return {
        str(row["task_id"]): ClaimHolder(
            task_id=str(row["task_id"]),
            task_seq=row["seq_num"],
            task_title=row["title"],
            session_id=str(row["session_id"]),
            agent=row["agent_name"],
            status=row["status"],
            claimed_at=row["claimed_at"],
            last_activity=row["last_activity"],
        )
        for row in rows
    }


def idle_holders(
    holders: Mapping[str, ClaimHolder],
    now: datetime,
    age_seconds: float,
) -> tuple[IdleHolder, ...]:
    result: list[IdleHolder] = []
    for holder in holders.values():
        age = (
            None
            if holder.last_activity is None
            else max(0.0, (now - holder.last_activity).total_seconds())
        )
        if holder.status != "active" or age is None or age >= age_seconds:
            result.append(
                IdleHolder(
                    task_seq=holder.task_seq,
                    task_title=holder.task_title,
                    session_id=holder.session_id,
                    agent=holder.agent,
                    status=holder.status,
                    idle_seconds=age,
                )
            )
    return tuple(sorted(result, key=lambda item: item.task_seq))


def _repair_state(db: HubDatabase, tree: Worktree) -> str:
    if tree.agent_session_id is None:
        return "unknown"
    instance = AgentStepInstanceManager(db).get_for_session(tree.agent_session_id)
    if instance is None:
        return "unknown"
    repair = instance.variables.get("integration_leaf_id")
    if repair is None:
        return "none recorded"
    if not isinstance(repair, str):
        return "unknown"
    row = db.fetchone(
        f"SELECT t.seq_num, ({projected_task_state_sql('t')}) AS state "
        "FROM tasks t WHERE t.project_id = %s "
        "AND (t.id::text = %s OR '#' || t.seq_num::text = %s) AND t.parent_task_id = %s",
        (tree.project_id, repair, repair, tree.task_id),
    )
    return "unknown" if row is None else f"#{row['seq_num']} {row['state']}"


def _landing_target(db: HubDatabase, task_id: str, integration_branch: str) -> str | None:
    """Project the admitted landing destination, independently of the tree's creation base."""
    from gobby.agents.team_admission_records import team_binding
    from gobby.agents.team_lifecycle import task_team
    from gobby.storage.team_records import EPIC_MODE

    team = task_team(db, task_id)
    if team and team.parent_team_id:
        parent = team_binding(db, team.parent_team_id)
        return None if parent is None else parent.branch
    if team and team.mode == EPIC_MODE and team.task_id != task_id:
        return team.branch
    return integration_branch


def enrich_rows(
    db: HubDatabase,
    project_id: str,
    rows: Sequence[WorktreeRow],
    worktrees: Sequence[Worktree],
    holders: Mapping[str, ClaimHolder],
    latest: Mapping[str, tuple[str, str, datetime]],
    integration_branch: str,
) -> tuple[WorktreeRow, ...]:
    tasks = {
        str(task["id"]): task
        for task in db.fetchall(
            "SELECT id, title, task_type, parent_task_id, closed_at FROM tasks WHERE project_id = %s",
            (project_id,),
        )
    }
    children: dict[str, list[str]] = defaultdict(list)
    for task_id, stored_task in tasks.items():
        if stored_task["parent_task_id"] is not None:
            children[str(stored_task["parent_task_id"])].append(task_id)

    def open_leaves(root: str) -> int:
        pending = list(children[root])
        seen = {root}
        count = 0
        while pending:
            task_id = pending.pop()
            if task_id in seen:
                continue
            seen.add(task_id)
            if children[task_id]:
                pending.extend(children[task_id])
            elif tasks[task_id]["closed_at"] is None:
                count += 1
        return count

    enriched: list[WorktreeRow] = []
    for row, tree in zip(rows, worktrees, strict=True):
        task_id = str(tree.task_id)
        task = tasks.get(task_id)
        holder = holders.get(task_id)
        attestation = latest.get(task_id)
        is_epic = task is not None and task["task_type"] == "epic"
        enriched.append(
            replace(
                row,
                task_title=None if task is None else task["title"],
                task_type=None if task is None else task["task_type"],
                holder_session_id=None if holder is None else holder.session_id,
                holder_agent=None if holder is None else holder.agent,
                holder_status=None if holder is None else holder.status,
                claimed_at=None if holder is None else holder.claimed_at,
                stage=None if attestation is None else f"{attestation[0]}/{attestation[1]}",
                open_leaf_count=open_leaves(task_id) if is_epic else None,
                landing_target=_landing_target(db, task_id, integration_branch),
                integration_leaf=_repair_state(db, tree) if is_epic else None,
            )
        )
    return tuple(
        sorted(
            enriched,
            key=lambda row: (
                row.task_type == "epic",
                row.task_seq is None,
                row.task_seq or 0,
                row.branch,
            ),
        )
    )
