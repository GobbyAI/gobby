"""Runtime mutex helpers for task stage-state mutations."""

from __future__ import annotations

from typing import cast

from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._runtime_mutex import RuntimeDispatchMutex, RuntimeStageSnapshotState
from gobby.storage.tasks._stage_state_rows import StageStateRows
from gobby.storage.tasks._stage_types import StageState, StageState5


class StageStateMutexFactory:
    def __init__(self, storage: TaskDispatchMutexManager, rows: StageStateRows) -> None:
        self.storage = storage
        self.rows = rows

    def mutex(
        self,
        task_id: str,
        holder: str,
        action: str,
        *,
        expected_stage: StageState | None = None,
    ) -> RuntimeDispatchMutex:
        if expected_stage is None:
            return RuntimeDispatchMutex(
                storage=self.storage,
                task_id=task_id,
                holder=holder,
                action_kind=f"stage_state:{action}",
                ttl_seconds=30,
            )
        return RuntimeDispatchMutex(
            storage=self.storage,
            task_id=task_id,
            holder=holder,
            action_kind=f"stage_state:{action}",
            ttl_seconds=30,
            expected_stage_name=expected_stage.stage_name,
            expected_stage_state=snapshot_state(expected_stage.state),
            expected_stage_updated_at=expected_stage.updated_at,
            candidate_loader=self.rows.current_stage,
        )


def snapshot_state(value: StageState5) -> RuntimeStageSnapshotState | None:
    if value in {"ready", "in_progress", "needs_review", "review_approved"}:
        return cast(RuntimeStageSnapshotState, value)
    return None
