"""Shared fixtures for stage-manifest storage contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from gobby.storage.tasks import LocalTaskManager


def require_stage_registry_types() -> tuple[type, type]:
    try:
        from gobby.storage.tasks._stage_registry import (
            StageRegistryEntry,
            StageRegistryManager,
        )
    except ImportError as exc:
        pytest.fail(f"Phase 2 stage registry module is missing: {exc}")
    return StageRegistryEntry, StageRegistryManager


def require_stage_state_types() -> dict[str, Any]:
    try:
        import gobby.storage.tasks._stage_states as module
    except ImportError as exc:
        pytest.fail(f"Phase 2 stage states module is missing: {exc}")

    required = {
        "IllegalManifestMutationError",
        "IllegalStageTransitionError",
        "StageManifestSpec",
        "StageState",
        "StageStatesManager",
    }
    missing = sorted(name for name in required if not hasattr(module, name))
    if missing:
        pytest.fail(f"Phase 2 stage states module is missing symbols: {missing}")
    return {name: getattr(module, name) for name in required} | {"module": module}


def stage_states_manager(db) -> Any:
    types = require_stage_state_types()
    return types["StageStatesManager"](db, LocalTaskManager(db).lifecycle_events)


def stage_registry_manager(db) -> Any:
    _, manager_cls = require_stage_registry_types()
    return manager_cls(db)


def create_task(db, sample_project: dict[str, Any], *, title: str = "Stage task", **kwargs: Any):
    return LocalTaskManager(db).create_task(
        project_id=sample_project["id"],
        title=title,
        **kwargs,
    )


def spec(stage_name: str, position: int, **kwargs: Any) -> Any:
    types = require_stage_state_types()
    return types["StageManifestSpec"](stage_name=stage_name, position=position, **kwargs)


def initialize_manifest(
    db,
    task_id: str,
    specs: Sequence[Any],
    *,
    by_session_id: str | None = "session-stage-tests",
) -> Any:
    return stage_states_manager(db).initialize_manifest(
        task_id,
        specs,
        by_session_id=by_session_id,
    )


def make_task_with_manifest(
    db,
    sample_project: dict[str, Any],
    specs: Sequence[Any],
    *,
    task_type: str = "feature",
    title: str = "Stage manifest task",
) -> tuple[Any, Any]:
    task = create_task(db, sample_project, title=title, task_type=task_type)
    manager = stage_states_manager(db)
    manager.initialize_manifest(task.id, specs, by_session_id="session-stage-tests")
    return task, manager


def set_stage_state(
    db,
    task_id: str,
    stage_name: str,
    state: str,
    *,
    review_policy: str | None = None,
    work_attempt_count: int | None = None,
    review_round_count: int | None = None,
) -> None:
    updates: dict[str, Any] = {"state": state}
    if review_policy is not None:
        updates["review_policy"] = review_policy
    if work_attempt_count is not None:
        updates["work_attempt_count"] = work_attempt_count
    if review_round_count is not None:
        updates["review_round_count"] = review_round_count
    assignments = ", ".join(f"{column} = ?" for column in updates)
    if stage_states_manager(db).get(task_id, stage_name) is None:
        initialize_manifest(db, task_id, [spec(stage_name, 0)])
    db.execute(
        f"""
        UPDATE task_stage_states
        SET {assignments}
        WHERE task_id = ? AND stage_name = ?
        """,  # nosec B608 - test-only helper with static caller-owned columns.
        (*updates.values(), task_id, stage_name),
    )


def stage_row(db, task_id: str, stage_name: str) -> dict[str, Any]:
    row = db.fetchone(
        """
        SELECT *
        FROM task_stage_states
        WHERE task_id = ? AND stage_name = ?
        """,
        (task_id, stage_name),
    )
    assert row is not None, f"missing stage row {task_id}:{stage_name}"
    return dict(row)


def stage_rows(db, task_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.fetchall(
            """
            SELECT *
            FROM task_stage_states
            WHERE task_id = ?
            ORDER BY position
            """,
            (task_id,),
        )
    ]


def lifecycle_events(db, task_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.fetchall(
            """
            SELECT from_state, to_state, reason, by_actor
            FROM task_lifecycle_events
            WHERE task_id = ?
            ORDER BY id
            """,
            (task_id,),
        )
    ]


def task_row(db, task_id: str) -> dict[str, Any]:
    row = db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    assert row is not None, f"missing task {task_id}"
    return dict(row)
