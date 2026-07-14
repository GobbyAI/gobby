"""Build dispatch-state cascade helpers."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES
from gobby.storage.hub.protocol import HubDatabase, TaskSubtreeCascade
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import Isolation
from gobby.storage.tasks._runtime_mutex import DispatchMutexUnavailableError, RuntimeDispatchMutex
from gobby.storage.tasks._stage_manifest import derive_child_manifest_specs
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._stage_types import ManifestAlreadyInitializedError, StageManifestSpec
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CascadeBuildFailure:
    """A task whose manifest could not be prepared for automation."""

    task_id: str
    error_type: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class CascadeBuildResult:
    """Outcome of applying build dispatch state to a task subtree."""

    updated_count: int
    failures: tuple[CascadeBuildFailure, ...] = ()


def cascade_build_state_to_subtree(
    db: HubDatabase,
    epic_id: str,
    isolation: Isolation | str,
    unattended: bool | None,
    allow_automation: bool,
    *,
    skip_stages: Iterable[str] = (),
    yolo: bool | None = None,
    parent_manifest_specs: Iterable[StageManifestSpec] | None = None,
    include_merge_stage: bool = False,
) -> CascadeBuildResult:
    """Apply build dispatch state to an epic and every descendant task.

    The cascade intentionally only touches dispatch controls and child manifest
    shape. Agent assignment, additional skills, and lifecycle fields remain
    task-local decisions.

    Returns the updated task count and any children that could not be prepared.
    """
    if unattended is None:
        unattended = bool(yolo)
    normalized_isolation = Isolation(isolation).value
    skipped_stages = set(skip_stages)
    now = utc_now()

    root_row = db.fetchone("SELECT project_id FROM tasks WHERE id = %s", (epic_id,))
    if root_row is None:
        raise ValueError(f"Task {epic_id} not found")
    project_id = cast(str, root_row["project_id"])

    with db.transaction_immediate(TaskSubtreeCascade(project_id=project_id)) as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE subtree(id, depth, path) AS (
                SELECT id, 0, ARRAY[id]
                FROM tasks
                WHERE id = %s
                UNION ALL
                SELECT child.id, parent.depth + 1, parent.path || child.id
                FROM tasks child
                JOIN subtree parent ON child.parent_task_id = parent.id
                WHERE parent.depth < 100
                  AND NOT child.id = ANY(parent.path)
            )
            SELECT id, task_type, closed_at
            FROM tasks
            WHERE id IN (SELECT id FROM subtree)
            """,
            (epic_id,),
        ).fetchall()

        if not rows:
            raise ValueError(f"Task {epic_id} not found")

    stage_states = StageStatesManager(db, TaskLifecycleEventManager(db))
    parent_specs: list[Any] = (
        list(parent_manifest_specs)
        if parent_manifest_specs is not None
        else stage_states.list_for_task(epic_id)
    )
    if skipped_stages:
        parent_specs = [spec for spec in parent_specs if spec.stage_name not in skipped_stages]
    ready_task_ids: list[str] = []
    failures: list[CascadeBuildFailure] = []
    for row in rows:
        task_id = cast(str, row["id"])
        if task_id != epic_id and row["closed_at"] is not None:
            continue

        if allow_automation and task_id == epic_id:
            if not stage_states.list_for_task(epic_id):
                failures.append(
                    CascadeBuildFailure(
                        task_id=task_id,
                        error_type="ManifestUnavailableError",
                        message="root task has no stage manifest",
                        retryable=False,
                    )
                )
                continue
        elif allow_automation:
            try:
                specs = derive_child_manifest_specs(
                    parent_specs,
                    include_holistic_qa=cast(str, row["task_type"]) == "epic",
                    include_merge_stage=include_merge_stage,
                )
                if not specs:
                    raise ValueError("no child manifest stages were derived")
                stage_states.initialize_manifest(task_id, specs, by_session_id=None)
            except DispatchMutexUnavailableError as exc:
                failures.append(
                    CascadeBuildFailure(
                        task_id=task_id,
                        error_type=type(exc).__name__,
                        message=str(exc),
                        retryable=True,
                    )
                )
                logger.warning("Build cascade could not acquire task %s: %s", task_id, exc)
                continue
            except ManifestAlreadyInitializedError:
                try:
                    if not _remove_pristine_omitted_stages_for_build_cascade(
                        db,
                        stage_states,
                        task_id,
                        specs,
                    ):
                        logger.info("Keeping progressed task %s during build cascade", task_id)
                except Exception as exc:
                    failures.append(
                        CascadeBuildFailure(
                            task_id=task_id,
                            error_type=type(exc).__name__,
                            message=str(exc),
                            retryable=False,
                        )
                    )
                    logger.exception("Build cascade failed to reconcile task %s", task_id)
                    continue
            except Exception as exc:
                failures.append(
                    CascadeBuildFailure(
                        task_id=task_id,
                        error_type=type(exc).__name__,
                        message=str(exc),
                        retryable=False,
                    )
                )
                logger.exception("Build cascade failed to prepare task %s", task_id)
                continue

        ready_task_ids.append(task_id)

    update_params = [
        (
            bool(allow_automation),
            bool(unattended),
            normalized_isolation,
            now,
            task_id,
        )
        for task_id in ready_task_ids
    ]
    with db.transaction_immediate(TaskSubtreeCascade(project_id=project_id)) as conn:
        conn.executemany(
            """
            UPDATE tasks
            SET allow_automation = %s,
                unattended = %s,
                isolation = %s,
                updated_at = %s
            WHERE id = %s
            """,
            update_params,
        )

    return CascadeBuildResult(updated_count=len(update_params), failures=tuple(failures))


def _remove_pristine_omitted_stages_for_build_cascade(
    db: HubDatabase,
    stage_states: StageStatesManager,
    task_id: str,
    desired_specs: Iterable[StageManifestSpec],
) -> bool:
    """Remove skipped ready stages from an existing build manifest.

    Build cascades may reshape already-created descendant manifests when a
    resumed expanded epic skips a delivery stage such as ``pr``. Only pristine
    ready rows are removable; progressed work stays untouched.
    """
    with RuntimeDispatchMutex(
        storage=stage_states.mutexes,
        task_id=task_id,
        holder="build",
        action_kind="stage_state:build_cascade_prune_manifest_stages",
        ttl_seconds=30,
    ):
        existing_rows = stage_states.list_for_task(task_id)
        desired = sorted(desired_specs, key=lambda spec: spec.position)
        if not existing_rows or not desired:
            return False

        existing_names = [row.stage_name for row in existing_rows]
        desired_names = [spec.stage_name for spec in desired]
        if existing_names == desired_names:
            return False
        if not _is_subsequence(desired_names, existing_names):
            return False

        desired_name_set = set(desired_names)
        current = stage_states.current_stage(task_id)
        has_active_agent = _has_active_agent_run(db, task_id)
        omitted_rows = [row for row in existing_rows if row.stage_name not in desired_name_set]
        if not omitted_rows or not all(
            _is_removable_omitted_stage(row, current, has_active_agent) for row in omitted_rows
        ):
            return False

        omitted_names = {row.stage_name for row in omitted_rows}
        removed_current = current is not None and current.stage_name in omitted_names
        remaining_rows = [row for row in existing_rows if row.stage_name in desired_name_set]
        if removed_current and not any(row.state != "done" for row in remaining_rows):
            return False

        previous_shape = ",".join(existing_names)
        desired_by_name = {spec.stage_name: spec for spec in desired}
        now = utc_now()
        with db.transaction() as conn:
            conn.executemany(
                "DELETE FROM task_stage_states WHERE task_id = %s AND stage_name = %s",
                [(task_id, row.stage_name) for row in omitted_rows],
            )
            for row in remaining_rows:
                spec = desired_by_name[row.stage_name]
                conn.execute(
                    """
                    UPDATE task_stage_states
                       SET position = %s,
                           max_work_attempts = %s,
                           max_review_rounds = %s,
                           updated_at = %s
                     WHERE task_id = %s AND stage_name = %s
                    """,
                    (
                        spec.position,
                        spec.max_work_attempts,
                        spec.max_review_rounds,
                        now,
                        task_id,
                        row.stage_name,
                    ),
                )
            if removed_current:
                conn.execute(
                    """
                    UPDATE tasks
                       SET claimed_by_session_id = NULL,
                           updated_at = %s
                     WHERE id = %s
                    """,
                    (now, task_id),
                )

        TaskLifecycleEventManager(db).record_lifecycle_event(
            task_id,
            from_state=f"manifest:{previous_shape}",
            to_state=f"manifest:{','.join(desired_names)}",
            reason="build_cascade_prune_manifest_stages",
            by_actor="build",
        )
        return True


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    cursor = iter(haystack)
    return all(any(candidate == stage_name for candidate in cursor) for stage_name in needle)


def _is_pristine_ready_stage(row: Any) -> bool:
    return (
        row.state == "ready"
        and row.entered_at is None
        and row.completed_at is None
        and row.work_attempt_count == 0
        and row.review_round_count == 0
        and row.artifact_refs is None
        and row.notes is None
    )


def _is_auto_started_without_agent(row: Any) -> bool:
    return (
        row.state == "in_progress"
        and row.entered_by_actor == "dispatcher"
        and row.completed_at is None
        and row.completed_by_session_id is None
        and row.completed_commit_sha is None
        and row.work_attempt_count == 1
        and row.review_round_count == 0
        and row.artifact_refs is None
        and row.notes is None
    )


def _is_removable_omitted_stage(row: Any, current: Any, has_active_agent: bool) -> bool:
    if _is_pristine_ready_stage(row):
        return True
    if current is None or row.stage_name != current.stage_name:
        return False
    return not has_active_agent and _is_auto_started_without_agent(row)


def _has_active_agent_run(db: HubDatabase, task_id: str) -> bool:
    placeholders = ", ".join("%s" for _ in ACTIVE_AGENT_RUN_STATUSES)
    row = db.fetchone(
        f"""
        SELECT 1
          FROM agent_runs
         WHERE task_id = %s
           AND status IN ({placeholders})
         LIMIT 1
        """,  # nosec B608 # placeholders are generated from static status constants.
        (task_id, *ACTIVE_AGENT_RUN_STATUSES),
    )
    return row is not None
