"""Planning-stage enhancement transitions."""

from __future__ import annotations

import re
from collections.abc import Sequence

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.tasks._artifacts import (
    _get_artifacts_in_transaction,
    _set_artifacts_in_transaction,
)
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._stage_types import NoCurrentStageError


def _stage_states(db: HubDatabase) -> StageStatesManager:
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def _enhancement_section_body(suggestions: Sequence[str], *, converged: bool) -> str:
    if suggestions:
        return "\n".join(f"- {item}" for item in suggestions)
    note = "Converged; no further suggestions." if converged else "No suggestions this round."
    return f"_{note}_"


def _fold_enhancement_round(
    existing: str,
    round_number: int,
    suggestions: Sequence[str],
    *,
    converged: bool,
) -> str:
    """Idempotently fold a round's enhancement section into the description.

    Re-running the same round replaces that round's section in place rather than
    stacking a duplicate heading, mirroring the adversary-round dedup behavior.
    """
    heading = f"## Enhancement Suggestions — Round {round_number}"
    section = f"{heading}\n\n{_enhancement_section_body(suggestions, converged=converged)}"
    exact_heading = rf"^{re.escape(heading)}$"
    if re.search(exact_heading, existing, re.MULTILINE):
        pattern = re.compile(
            exact_heading + r".*?(?=^## Enhancement Suggestions — Round [0-9]+$|\Z)",
            re.DOTALL | re.MULTILINE,
        )
        replacement = section.rstrip() + "\n\n"
        return pattern.sub(lambda _match: replacement, existing).rstrip() or section
    return f"{existing}\n\n{section}" if existing else section


def _persist_enhancement(
    conn: Transaction,
    task_id: str,
    *,
    round_number: int,
    converged: bool,
    suggestions: Sequence[str],
) -> None:
    task_row = conn.execute(
        "SELECT description FROM tasks WHERE id = %s FOR UPDATE",
        (task_id,),
    ).fetchone()
    if task_row is None:
        raise ValueError(f"Task {task_id} not found")

    conn.execute(
        "INSERT INTO task_artifacts (task_id) VALUES (%s) ON CONFLICT(task_id) DO NOTHING",
        (task_id,),
    )
    artifacts = _get_artifacts_in_transaction(conn, task_id, for_update=True)
    _set_artifacts_in_transaction(
        conn,
        task_id,
        {
            "plan_enhancement_rounds_completed": max(
                artifacts.plan_enhancement_rounds_completed,
                round_number,
            ),
            "plan_enhancement_converged": converged,
        },
    )

    description = _fold_enhancement_round(
        task_row["description"] or "",
        round_number,
        suggestions,
        converged=converged,
    )
    cursor = conn.execute(
        """
        UPDATE tasks
           SET description = %s,
               claimed_by_session_id = NULL,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        (description, task_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Task {task_id} not found")


def record_plan_enhancement(
    db: HubDatabase,
    task_id: str,
    *,
    round_number: int,
    converged: bool,
    suggestions: Sequence[str] | None = None,
    signoff_summary: str | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Record a constructive plan-enhancement round on the planning stage.

    Enhancement is tracked independently of the adversary review budget. When
    ``suggestions`` are present the planning stage returns from ``needs_review``
    to ``ready`` so the planner folds them in, WITHOUT incrementing
    ``review_round_count``. When the round converges or yields no suggestions the
    stage stays in ``needs_review`` so the adversary gate proceeds. Either way the
    enhancement counters are persisted and the claim is released.
    """
    normalized_round = int(round_number)
    if normalized_round < 1:
        raise ValueError("round_number must be >= 1")

    get_task(db, task_id)
    stages = _stage_states(db)
    current = stages.current_stage(task_id)
    if current is None:
        raise NoCurrentStageError(task_id)
    if current.stage_name != "planning":
        raise ValueError(
            "record_plan_enhancement requires the planning stage to be current; "
            f"current stage is '{current.stage_name}'"
        )
    if current.state != "needs_review":
        raise ValueError(
            "record_plan_enhancement requires the planning stage to be in needs_review; "
            f"current state is '{current.state}'"
        )

    cleaned = [item.strip() for item in (suggestions or []) if item and item.strip()]
    has_suggestions = bool(cleaned)

    def persist(conn: Transaction) -> None:
        _persist_enhancement(
            conn,
            task_id,
            round_number=normalized_round,
            converged=converged,
            suggestions=cleaned,
        )

    if has_suggestions:
        # Route the plan back to the planner; route_enhancement intentionally
        # does NOT bump review_round_count (only reject_review does).
        stages.route_enhancement(
            task_id,
            current.stage_name,
            by_session_id=by_session_id,
            notes=signoff_summary,
            _transaction_mutation=persist,
        )
    else:
        with db.transaction() as conn:
            persist(conn)
    return get_task(db, task_id)
