from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._holistic_gate import (
    HOLISTIC_DESCENDANT_GATE_REASON,
    find_holistic_descendant_gate,
)
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit


def test_holistic_descendant_gate_blocks_open_ready_descendant(temp_db, sample_project) -> None:
    root = _holistic_root(temp_db, sample_project)
    child = _child(temp_db, sample_project, root.id, stage_state="ready")

    gate = _gate(temp_db, root.id)

    assert gate is not None
    assert gate.reason == HOLISTIC_DESCENDANT_GATE_REASON
    assert [blocker.task_id for blocker in gate.blockers] == [child.id]
    assert gate.blockers[0].stage_name == "development"
    assert gate.blockers[0].stage_state == "ready"
    assert gate.blockers[0].reason == HOLISTIC_DESCENDANT_GATE_REASON


def test_holistic_descendant_gate_blocks_escalated_descendant_without_stage(
    temp_db,
    sample_project,
) -> None:
    root = _holistic_root(temp_db, sample_project)
    child = create_task(temp_db, sample_project, parent_task_id=root.id, title="Escalated child")
    LocalTaskManager(temp_db).escalate_task(child.id, "needs_human")

    gate = _gate(temp_db, root.id)

    assert gate is not None
    assert [blocker.task_id for blocker in gate.blockers] == [child.id]
    assert gate.blockers[0].is_escalated is True
    assert gate.blockers[0].stage_name is None
    assert gate.blockers[0].stage_state is None


def test_holistic_descendant_gate_blocks_nested_descendant(temp_db, sample_project) -> None:
    root = _holistic_root(temp_db, sample_project)
    phase = create_task(temp_db, sample_project, parent_task_id=root.id, title="Integrated phase")
    child = _child(temp_db, sample_project, phase.id, stage_state="in_progress")

    gate = _gate(temp_db, root.id)

    assert gate is not None
    assert [blocker.task_id for blocker in gate.blockers] == [child.id]
    assert gate.blockers[0].stage_state == "in_progress"


def test_holistic_descendant_gate_allows_closed_or_terminal_descendants(
    temp_db,
    sample_project,
) -> None:
    root = _holistic_root(temp_db, sample_project)
    manager = LocalTaskManager(temp_db)
    closed = _child(temp_db, sample_project, root.id, title="Closed child", stage_state="ready")
    terminal = _child(temp_db, sample_project, root.id, title="Terminal child", stage_state="done")
    set_stage_state(temp_db, terminal.id, "merge", "done")
    manager.close_task(closed.id, force=True)

    gate = _gate(temp_db, root.id)

    assert terminal.id
    assert gate is None


def _holistic_root(temp_db, sample_project, *, stage_state: str = "ready"):
    root = create_task(
        temp_db,
        sample_project,
        title="Holistic root",
        task_type="epic",
        allow_automation=True,
        isolation="none",
    )
    initialize_manifest(
        temp_db,
        root.id,
        [spec("development", 0), spec("holistic_qa", 1), spec("merge", 2)],
    )
    set_stage_state(temp_db, root.id, "development", "done")
    set_stage_state(temp_db, root.id, "holistic_qa", stage_state)
    return LocalTaskManager(temp_db).get_task(root.id)


def _child(
    temp_db,
    sample_project,
    parent_task_id: str,
    *,
    title: str = "Descendant",
    stage_state: str,
):
    child = create_task(
        temp_db,
        sample_project,
        parent_task_id=parent_task_id,
        title=title,
        category="code",
    )
    initialize_manifest(temp_db, child.id, [spec("development", 0), spec("merge", 1)])
    set_stage_state(temp_db, child.id, "development", stage_state)
    return child


def _gate(temp_db, root_id: str):
    manager = LocalTaskManager(temp_db)
    root = manager.get_task(root_id)
    return find_holistic_descendant_gate(
        temp_db,
        root,
        current_stage=manager.stage_states.current_stage(root.id),
    )
