"""Dispatch-side bounds for retry-neutral cycles (gobby-#17668).

Part 3: ``_stage_work_exhausted`` keeps its strict ``> cap`` comparison. That is
the same cap-attempt bound the storage layer enforces with ``>=``; the operators
differ only because the dispatcher checks before dispatching the in-flight
attempt (count already incremented by ``start_stage``) while storage escalates
after that attempt fails. Switching the dispatcher to ``>=`` would escalate
before the final attempt ran and regress allowed attempts from ``cap`` to
``cap-1``.

Part 4: the internal stage-pipeline mutex race is retry-neutral, but a persistent
race now escalates after a small consecutive-restore ceiling instead of relying
on the mutex lease eventually expiring. A successful attach resets the counter.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.dispatch._rule_state import _stage_work_exhausted
from gobby.dispatch.dispatcher import _stage_states_manager
from gobby.dispatch.stage_pipeline import (
    MAX_PIPELINE_RETRY_NEUTRAL_RESTORES,
    reset_stage_pipeline_retry_neutral,
    restore_stage_pipeline_retry,
    retry_neutral_pipeline_dispatch,
)
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import (
    initialize_manifest,
    set_stage_state,
    spec,
    stage_row,
)

pytestmark = pytest.mark.unit


# --- Part 3: work-exhaustion comparison alignment -----------------------------


def test_stage_work_not_exhausted_at_cap_allows_in_flight_attempt() -> None:
    # count == cap: start_stage already bumped the counter for the cap-th
    # attempt, which is now in flight and must be allowed to run. Storage's >=
    # escalates only after that attempt fails, so the two agree on `cap`
    # attempts. See gobby-#17668.
    stage = SimpleNamespace(stage_name="development", max_work_attempts=2, work_attempt_count=2)
    assert _stage_work_exhausted(stage, None) is False


def test_stage_work_exhausted_above_cap() -> None:
    stage = SimpleNamespace(stage_name="development", max_work_attempts=2, work_attempt_count=3)
    assert _stage_work_exhausted(stage, None) is True


def test_stage_work_not_exhausted_below_cap() -> None:
    stage = SimpleNamespace(stage_name="development", max_work_attempts=2, work_attempt_count=1)
    assert _stage_work_exhausted(stage, None) is False


def test_stage_work_exhausted_none_cap_is_false() -> None:
    stage = SimpleNamespace(stage_name="development", max_work_attempts=None, work_attempt_count=99)
    assert _stage_work_exhausted(stage, None) is False


# --- Part 4: pipeline retry-neutral ceiling -----------------------------------


def _task_with_pipeline_stage(temp_db, sample_project) -> str:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Pipeline task",
        task_type="task",
    )
    initialize_manifest(temp_db, task.id, [spec("expansion", 0)])
    return task.id


def test_restore_increments_counter_and_stays_attempt_neutral(temp_db, sample_project) -> None:
    task_id = _task_with_pipeline_stage(temp_db, sample_project)

    counts: list[int] = []
    for _ in range(3):
        set_stage_state(temp_db, task_id, "expansion", "in_progress", work_attempt_count=1)
        counts.append(
            restore_stage_pipeline_retry(
                temp_db,
                task_id,
                "expansion",
                reason="mutex held",
                stage_states_manager=_stage_states_manager,
            )
        )

    assert counts == [1, 2, 3]
    row = stage_row(temp_db, task_id, "expansion")
    assert row["state"] == "ready"
    assert row["work_attempt_count"] == 0
    assert row["retry_neutral_failure_count"] == 3


def test_restore_returns_zero_when_stage_not_in_progress(temp_db, sample_project) -> None:
    task_id = _task_with_pipeline_stage(temp_db, sample_project)
    set_stage_state(temp_db, task_id, "expansion", "ready")

    result = restore_stage_pipeline_retry(
        temp_db,
        task_id,
        "expansion",
        reason="mutex held",
        stage_states_manager=_stage_states_manager,
    )

    assert result == 0
    assert stage_row(temp_db, task_id, "expansion")["retry_neutral_failure_count"] == 0


def test_successful_attach_resets_counter(temp_db, sample_project) -> None:
    task_id = _task_with_pipeline_stage(temp_db, sample_project)
    set_stage_state(temp_db, task_id, "expansion", "in_progress", work_attempt_count=1)
    restore_stage_pipeline_retry(
        temp_db,
        task_id,
        "expansion",
        reason="mutex held",
        stage_states_manager=_stage_states_manager,
    )
    assert stage_row(temp_db, task_id, "expansion")["retry_neutral_failure_count"] == 1

    reset_stage_pipeline_retry_neutral(temp_db, task_id, "expansion")

    assert stage_row(temp_db, task_id, "expansion")["retry_neutral_failure_count"] == 0


def _pipeline_action() -> SimpleNamespace:
    return SimpleNamespace(task_id="t1", stage_name="expansion")


def test_retry_neutral_dispatch_below_ceiling_stays_retry_neutral() -> None:
    released: list[bool] = []
    escalations: list[str] = []
    mutex = SimpleNamespace(release=lambda: released.append(True))

    result = retry_neutral_pipeline_dispatch(
        _pipeline_action(),
        mutex,
        None,
        "mutex held",
        restore_stage_pipeline_retry=lambda *a, **k: 1,
        escalate_task=lambda **kw: escalations.append(kw["reason"]),
    )

    assert result["retry_neutral"] is True
    assert "escalated" not in result
    assert escalations == []
    assert released == [True]


def test_retry_neutral_dispatch_escalates_at_ceiling() -> None:
    released: list[bool] = []
    escalations: list[tuple[str, str]] = []
    mutex = SimpleNamespace(release=lambda: released.append(True))

    def fake_escalate(*, db: object, task_id: str, reason: str) -> bool:
        escalations.append((task_id, reason))
        return True

    result = retry_neutral_pipeline_dispatch(
        _pipeline_action(),
        mutex,
        None,
        "mutex held",
        restore_stage_pipeline_retry=lambda *a, **k: MAX_PIPELINE_RETRY_NEUTRAL_RESTORES,
        escalate_task=fake_escalate,
    )

    assert result["retry_neutral"] is False
    assert result["escalated"] is True
    assert released == [True]
    assert len(escalations) == 1
    task_id, reason = escalations[0]
    assert task_id == "t1"
    assert reason.startswith("stage_pipeline_dispatch_retry_neutral:max:")
