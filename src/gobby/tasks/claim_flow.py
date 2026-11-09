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
    from collections.abc import Callable

    from gobby.tasks.attestations import TaskAttestation
    from gobby.workflows.pipeline_models import ClaimStage


def _is_team_manager(db: HubDatabase, agent: str, project_id: str) -> bool:
    from gobby.workflows.agent_resolver import resolve_agent_with_row

    resolved = resolve_agent_with_row(agent, db, project_id=project_id)
    return (
        resolved is not None
        and resolved[0].enabled
        and resolved[1].enabled
        and resolved[0].workflows.variables.get("team_manager") is True
    )


def _reference_key(item: TaskAttestation) -> tuple[str, str] | None:
    if item.reference is None:
        return None
    return (item.reference.scheme, item.reference.value)


def _by_agent(agent: str) -> Callable[[TaskAttestation], bool]:
    return lambda item: item.agent_definition == agent


def _current_by_kind(
    records: list[TaskAttestation], kinds: list[str], belongs: Callable[[TaskAttestation], bool]
) -> dict[str, TaskAttestation]:
    """The latest unsuperseded record per kind, among records the predicate admits."""
    superseded = {item.supersedes_id for item in records if item.supersedes_id}
    current: dict[str, TaskAttestation] = {}
    for item in records:
        if item.id not in superseded and item.kind in kinds and belongs(item):
            current[item.kind] = item
    return current


def _approved_on(
    current: dict[str, TaskAttestation],
    kinds: list[str],
    candidate: tuple[str, str],
    approved: str,
    unreferenced: str,
) -> bool:
    """Every kind is approved on the candidate. Only the record `unreferenced` may carry none."""
    for kind in kinds:
        item = current.get(kind)
        if item is None or item.result != approved:
            return False
        key = _reference_key(item)
        if key != candidate and not (key is None and item.id == unreferenced):
            return False
    return True


def _routes(definition: PipelineDefinition, name: str, category: str | None) -> list[str]:
    """Alternatives a task may take from a stage, in declared order, filtered by its category."""
    return [
        following
        for following in definition.claim_flow[name].next
        if _admits(definition, following, category)
    ]


def _route_between(
    definition: PipelineDefinition, start: str, goal: str, category: str | None
) -> list[str] | None:
    """The stages strictly between start and goal on the route a task takes: the first admitted
    alternative at each stage, as advancement picks it. None when that route never reaches goal."""
    between: list[str] = []
    current = start
    while True:
        taken = _routes(definition, current, category)[:1]
        if not taken:
            return None
        following = taken[0]
        if following == goal:
            return between
        if following == start or following in between:
            return None
        between.append(following)
        current = following


def _returned_stage(
    definition: PipelineDefinition,
    holder_stage: str,
    marked: str,
    correction: TaskAttestation,
    records: list[TaskAttestation],
    stage_agents: dict[str, str | None],
    category: str | None,
) -> str | None:
    """The marked stage when a correction re-records a kind on an already reviewed candidate.

    The correction must supersede one of the holder's records. The candidate is the one reference
    on which every done_when kind of the holder's stage is approved, and only the correction may
    carry no reference. Each stage between the holder's stage and the marked stage must already be
    approved on that candidate by its own writers. A fresh candidate fails that check, so it still
    needs review.
    """
    holder_ids = {item.id for item in records if item.session_id == correction.session_id}
    if correction.supersedes_id not in holder_ids:
        return None
    holder = definition.claim_flow[holder_stage]
    if correction.kind not in holder.done_when:
        return None
    current = _current_by_kind(
        records, holder.done_when, lambda item: item.session_id == correction.session_id
    )
    candidates: set[tuple[str, str]] = set()
    for item in current.values():
        key = _reference_key(item)
        if key is not None and _approved_on(
            current, holder.done_when, key, holder.approved_result, correction.id
        ):
            candidates.add(key)
    if len(candidates) != 1:
        return None
    (candidate,) = candidates

    def writers_approved(name: str) -> bool:
        stage = definition.claim_flow[name]
        writer = stage_agents.get(name)
        if writer is None:
            return False
        current_stage = _current_by_kind(records, stage.done_when, _by_agent(writer))
        return _approved_on(current_stage, stage.done_when, candidate, stage.approved_result, "")

    # The marked stage must lie beyond at least one stage, all on the route this task takes. Each
    # of those stages must be approved on the candidate by its own writers.
    between = _route_between(definition, holder_stage, marked, category)
    if between and all(writers_approved(name) for name in between):
        return marked
    return None


def _admits(definition: PipelineDefinition, name: str, category: str | None) -> bool:
    categories = definition.claim_flow[name].categories
    return not categories or category in categories


def _produced(
    records: list[TaskAttestation],
    stage: ClaimStage,
    writer: str | None,
    candidate: tuple[str, str] | None,
) -> bool:
    """Whether the stage's writer approved every done_when kind, all on the candidate. test-before
    is recorded before the candidate exists, so it may carry no reference."""
    if writer is None or candidate is None or not stage.done_when:
        return False
    current = _current_by_kind(records, stage.done_when, _by_agent(writer))
    if any(
        kind not in current or current[kind].result != stage.approved_result
        for kind in stage.done_when
    ):
        return False
    references = {_reference_key(current[kind]) for kind in stage.done_when} - {None}
    return references == {candidate}


def _seat_for_task(
    stage_name: str,
    task: Task,
    seats: dict[str, str | None],
    *,
    home: str,
    child: str | None,
) -> str | None:
    """The live seat that serves ``task`` for ``stage_name``; ``seats`` maps session to run task.

    A seat whose run names another leaf of the team serves that leaf, never this task. Among the
    rest, the seats bound to this task win: its claim holder, its run's task, or the admission's
    step output when that run names no task. A run that names only the team's task is shared by
    the team's leaves, so it never binds one of them. Unbound seats serve only when none is bound.
    Two seats left in the chosen set are ambiguous, and the error names the task and both sessions.
    """
    eligible = {
        sid: run_task for sid, run_task in seats.items() if run_task in (None, home, task.id)
    }
    bound = {
        sid
        for sid, run_task in eligible.items()
        if run_task == task.id
        or sid == task.claimed_by_session_id
        or (run_task is None and sid == child)
    }
    pool = bound or set(eligible)
    if len(pool) > 1:
        raise ValueError(
            f"claim flow stage {stage_name!r} has ambiguous live pipeline seats for task "
            f"{task.id}: {', '.join(sorted(pool))}"
        )
    return next(iter(pool)) if pool else None


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
    seat_steps = {step.id: step for step in definition.steps}
    steps = {step.step_id: step for step in executions.get_steps_for_execution(team.team_id)}

    def seat_agent(stage_name: str) -> str | None:
        stage = definition.claim_flow[stage_name]
        if stage.seat is None:
            return stage.agent
        step = seat_steps[stage.seat]
        return (step.mcp.arguments or {}).get("agent") if step.mcp else None

    def stage_session(stage_name: str) -> str | None:
        seat = definition.claim_flow[stage_name].seat
        step = steps.get(seat) if seat is not None else None
        output = json.loads(step.output_json) if step and step.output_json else {}
        child = output.get("child_session_id")
        # Relaunches join the same admission; their active runs replace ended step outputs.
        agent = seat_agent(stage_name)
        live: dict[str, str | None] = {}
        home = team.task_id
        if agent:
            rows = conn.execute(
                "SELECT ar.child_session_id, ar.task_id FROM agent_runs ar "
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
            seated = {
                str(row["child_session_id"]): (
                    seat_team(db, str(row["child_session_id"])),
                    row["task_id"],
                )
                for row in rows
            }
            live = {
                sid: run_task
                for sid, (member, run_task) in seated.items()
                if member and member.team_id == team.team_id
            }
            # A pod launches no manager seat; its parent team's manager holds that stage.
            if not live and child is None and _is_team_manager(db, agent, task.project_id):
                from gobby.agents.team_admission_records import team_binding

                binding = team_binding(db, team.team_id)
                parent = binding.parent_team_id if binding is not None else None
                parent_binding = team_binding(db, parent) if parent is not None else None
                if parent_binding is not None:
                    home = parent_binding.task_id
                    live = {
                        sid: run_task
                        for sid, (member, run_task) in seated.items()
                        if member and member.team_id == parent
                    }
        seat_id = _seat_for_task(stage_name, task, live, home=home, child=child)
        if seat_id is None and child in live:
            # The admitted seat runs another leaf of this team; it never serves this task.
            return None
        return seat_id if seat_id is not None else child

    # Resolve the stage this author holds. A marker names the stage last moved to; when its
    # recipient is this author, the marker wins even after a relaunch gave the seat a newer live
    # session, and even when one seat runs several stages. With no marker, the first stage is the
    # initial claim. Otherwise a direct claim or an explicit hand-off changed the holder without a
    # marker, so the author's own seat picks the stage. Zero or several matches refuse the evidence
    # and roll it back; they never leave the claim silently unmoved.
    first = next(iter(definition.claim_flow))
    stage_agents = {stage_name: seat_agent(stage_name) for stage_name in definition.claim_flow}
    marked = state.get("stage")
    returned: str | None = None
    if (
        prior is not None
        and state.get("to_session_id") == record.session_id
        and marked in definition.claim_flow
    ):
        name = marked
    elif (
        prior is None
        and seat_agent(first) == record.agent_definition
        and stage_session(first) == record.session_id
    ):
        name = first
    else:
        holder_stages = [
            stage_name
            for stage_name in definition.claim_flow
            if seat_agent(stage_name) == record.agent_definition
            and stage_session(stage_name) == record.session_id
        ]
        if not holder_stages:
            raise ValueError(f"claim flow holder {record.session_id} has no claim flow stage")
        if len(holder_stages) > 1:
            raise ValueError(f"claim flow seat {record.session_id} holds more than one stage")
        name = holder_stages[0]
        if prior is not None and marked in definition.claim_flow:
            returned = _returned_stage(
                definition, name, str(marked), record, records, stage_agents, task.category
            )
    stage = definition.claim_flow[name]
    superseded = {item.supersedes_id for item in records if item.supersedes_id}
    active = {
        item.kind: item
        for item in records
        if item.id not in superseded
        and item.session_id == record.session_id
        and item.sequence > state.get("after_sequence", 0)
    }
    targets: list[str] = []
    references = {
        (item.reference.scheme, item.reference.value)
        for kind, item in active.items()
        if kind in stage.done_when and item.reference is not None
    }
    if returned is not None:
        targets = [returned]
    elif record.kind in stage.done_when and record.result == stage.rejected_result:
        targets = stage.on_rejected
        if len(targets) > 1:
            # Several lanes can take a rejection back; only the lane whose writer produced the
            # rejected candidate takes it, so no other lane's seat is ever picked by liveness.
            candidate = _reference_key(record)
            producers = [
                lane
                for lane in targets
                if _produced(records, definition.claim_flow[lane], stage_agents[lane], candidate)
            ]
            if len(producers) != 1:
                raise ValueError(
                    f"claim flow stage {name!r} cannot tell which lane produced the candidate"
                )
            targets = producers
    elif stage.done_when and all(
        kind in active and active[kind].result == stage.approved_result for kind in stage.done_when
    ):
        if stage.next and len(references) > 1:
            latest = max(
                (active[kind] for kind in stage.done_when if active[kind].reference is not None),
                key=lambda item: item.sequence,
            )
            reference = latest.reference
            if reference is None:
                raise ValueError("claim_flow reference is unavailable")
            conflicting = ", ".join(
                f"{kind}={item.reference.scheme}:{item.reference.value}"
                for kind, item in active.items()
                if kind in stage.done_when and item.reference is not None
            )
            raise ValueError(
                f"claim_flow stage {name!r} requires one reference across done_when evidence. "
                f"Accepted reference for this attempt: {reference.scheme}:{reference.value}. "
                f"Supersede conflicting records or omit references for unrelated evidence: {conflicting}"
            )
        # The first alternative whose categories admit this task takes the claim, in declared order.
        routes = _routes(definition, name, task.category)
        if stage.next and not routes:
            raise ValueError(
                f"claim flow stage {name!r} has no route for category {task.category!r}"
            )
        targets = routes[:1]
    if not targets:
        return
    target = targets[0]
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
