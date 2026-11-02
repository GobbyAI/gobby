"""Apply a runbook's declarative claim stages inside the attestation transaction."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import LIVE_SESSION_STATUSES, SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.pipeline_models import PipelineDefinition

if TYPE_CHECKING:
    from gobby.tasks.attestations import TaskAttestation


def advance_claim_flow(
    db: HubDatabase,
    conn: Transaction,
    task: Task,
    record: TaskAttestation,
    records: list[TaskAttestation],
) -> None:
    """Only the holder's current stage can move the claim; a failed move rolls back evidence."""
    if task.claimed_by_session_id != record.session_id:
        return
    from gobby.agents.team_lifecycle import seat_team

    team = seat_team(db, record.session_id)
    if team is None:
        return
    executions = LocalPipelineExecutionManager(db, task.project_id)
    execution = executions.get_execution(team.team_id)
    if execution is None or not execution.definition_json:
        return
    definition = PipelineDefinition.model_validate_json(execution.definition_json)
    if not definition.claim_flow:
        return
    # The admission must cover this task, including a persistent epic team's leaves.
    if task.id != team.task_id:
        from gobby.agents.team_scope import open_leaf_below

        if team.mode != "epic" or open_leaf_below(db, team.task_id, task.id) is None:
            raise ValueError("claim flow does not cover this task")
    session_store = SessionManager(db)
    author = session_store.get(record.session_id)
    if author is None:
        raise ValueError("claim flow author session is missing")
    prior = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = %s AND author_type = %s "
        "AND body::jsonb ->> 'team_id' = %s "
        "ORDER BY (body::jsonb ->> 'after_sequence')::bigint DESC LIMIT 1",
        (task.id, "task_claim_flow", team.team_id),
    ).fetchone()
    state = json.loads(prior["body"]) if prior else {}
    name = state.get("stage") or next(iter(definition.claim_flow))
    stage = definition.claim_flow[name]
    seat_steps = {step.id: step for step in definition.steps}
    steps = {step.step_id: step for step in executions.get_steps_for_execution(team.team_id)}

    def stage_session(stage_name: str) -> str | None:
        seat = definition.claim_flow[stage_name].seat
        step = steps.get(seat)
        output = json.loads(step.output_json) if step and step.output_json else {}
        child = output.get("child_session_id")
        # Relaunches join the same admission; their active runs replace ended step outputs.
        seat_step = seat_steps[seat]
        agent = (seat_step.mcp.arguments or {}).get("agent") if seat_step.mcp else None
        live: set[str] = set()
        if agent:
            rows = conn.execute(
                "SELECT ar.child_session_id FROM agent_runs ar "
                "JOIN sessions s ON s.id = ar.child_session_id "
                "WHERE ar.status IN ('queued', 'running', 'pending') "
                "AND ar.machine_id = %s AND ar.agent_name = %s "
                "AND s.project_id = %s AND s.machine_id = %s AND s.status = ANY(%s)",
                (
                    author.machine_id,
                    agent,
                    task.project_id,
                    author.machine_id,
                    list(LIVE_SESSION_STATUSES),
                ),
            ).fetchall()
            for row in rows:
                child_id = str(row["child_session_id"])
                child_team = seat_team(db, child_id)
                if child_team is not None and child_team.team_id == team.team_id:
                    live.add(child_id)
        if len(live) > 1:
            raise ValueError(f"claim flow stage {stage_name!r} has ambiguous live pipeline seats")
        return next(iter(live)) if live else child

    if stage_session(name) != record.session_id:
        return
    superseded = {item.supersedes_id for item in records if item.supersedes_id}
    active = {
        item.kind: item
        for item in records
        if item.id not in superseded
        and item.session_id == record.session_id
        and item.sequence > state.get("after_sequence", 0)
    }
    target = None
    references = {
        (item.reference.scheme, item.reference.value)
        for kind, item in active.items()
        if kind in stage.done_when and item.reference is not None
    }
    if record.kind in stage.done_when and record.result == stage.rejected_result:
        target = stage.on_rejected
    elif (
        stage.done_when
        and len(references) <= 1
        and all(
            kind in active and active[kind].result == stage.approved_result
            for kind in stage.done_when
        )
    ):
        target = stage.next
    if target is None:
        return
    recipient_id = stage_session(target)
    recipient = session_store.get(recipient_id) if recipient_id else None
    if (
        recipient is None
        or recipient.project_id != task.project_id
        or recipient.machine_id != author.machine_id
        or recipient.status not in LIVE_SESSION_STATUSES
    ):
        raise ValueError(f"claim flow stage {target!r} needs its live pipeline seat")
    LocalTaskManager(db).claim_task_for_agent(
        task.id, recipient.id, expected_owner=record.session_id, handed=True
    )
    conn.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            str(uuid4()),
            task.id,
            record.session_id,
            "task_claim_flow",
            json.dumps(
                {
                    "team_id": team.team_id,
                    "stage": target,
                    "after_sequence": record.sequence,
                    "from_session_id": record.session_id,
                    "to_session_id": recipient.id,
                }
            ),
        ),
    )
