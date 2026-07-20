"""Planning-stage enhancement transitions."""

from __future__ import annotations

import re
from collections.abc import Sequence

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._artifacts import get_artifacts, set_artifacts_atomic
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._stage_types import NoCurrentStageError
from gobby.storage.tasks._updates import update_task


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
    if heading in existing:
        pattern = re.compile(
            rf"^{re.escape(heading)}.*?(?=^## Enhancement Suggestions — Round |\Z)",
            re.DOTALL | re.MULTILINE,
        )
        return pattern.sub(section.rstrip() + "\n\n", existing).rstrip() or section
    return f"{existing}\n\n{section}" if existing else section


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

    task = get_task(db, task_id)
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

    # Enhancement counters live in task_artifacts, independent of review rounds.
    artifacts = get_artifacts(db, task_id)
    set_artifacts_atomic(
        db,
        task_id,
        plan_enhancement_rounds_completed=max(
            artifacts.plan_enhancement_rounds_completed, normalized_round
        ),
        plan_enhancement_converged=converged,
    )

    description = _fold_enhancement_round(
        task.description or "",
        normalized_round,
        cleaned,
        converged=converged,
    )

    if has_suggestions:
        # Route the plan back to the planner; route_enhancement intentionally
        # does NOT bump review_round_count (only reject_review does).
        stages.route_enhancement(
            task_id,
            current.stage_name,
            by_session_id=by_session_id,
            notes=signoff_summary,
        )

    update_task(
        db,
        task_id,
        description=description,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)
