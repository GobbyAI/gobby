from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

pytestmark = pytest.mark.unit

VALIDATION_CRITERIA = "Storage fixture task; behavior asserted by the test."


def test_build_history_records_runs_and_events_in_newest_order(temp_db) -> None:
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("build-history", repo_path="/tmp/history")
    task = LocalTaskManager(temp_db).create_task(project.id, "Build root", task_type="epic")
    history = BuildHistoryStorage(temp_db)

    first = history.record_run(
        project_id=project.id,
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"step": 1},
    )
    second = history.record_run(
        project_id=project.id,
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="resume",
        summary={"step": 2},
    )
    history.record_event(
        run_id=first.id,
        project_id=project.id,
        root_task_id=task.id,
        task_id=task.id,
        event_type="build_completed",
        action="build",
        payload={"ok": True},
    )
    history.record_event(
        run_id=second.id,
        project_id=project.id,
        root_task_id=task.id,
        task_id=task.id,
        event_type="task_build_control",
        action="resume",
        payload={"ok": True},
    )

    assert [run.id for run in history.list_runs(project_id=project.id, root_task_id=task.id)] == [
        second.id,
        first.id,
    ]
    events = history.list_events(project_id=project.id, root_task_id=task.id)
    assert [event.action for event in events] == ["resume", "build"]
    assert events[0].payload == {"ok": True}


def test_build_history_get_event_returns_none_for_missing_event(temp_db) -> None:
    from gobby.storage.build_history import BuildHistoryStorage

    assert BuildHistoryStorage(temp_db).get_event(999_999) is None


def test_build_history_get_run_returns_none_for_missing_run(temp_db) -> None:
    from gobby.storage.build_history import BuildHistoryStorage

    assert BuildHistoryStorage(temp_db).get_run("00000000-0000-0000-0000-0000000000ff") is None


def test_build_history_start_and_finish_updates_root_and_status(temp_db) -> None:
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("build-history-finish", repo_path="/tmp/history")
    task = LocalTaskManager(temp_db).create_task(
        project.id, "Build root", validation_criteria=VALIDATION_CRITERIA
    )
    history = BuildHistoryStorage(temp_db)

    run = history.start_run(project_id=project.id, input_ref="plan.md", action="build")
    finished = history.finish_run(
        run.id,
        status="completed",
        root_task_id=task.id,
        summary={"task_id": task.id},
    )

    assert finished.status == "completed"
    assert finished.root_task_id == task.id
    assert finished.completed_at is not None
    assert history.latest_run_for_input(project.id, "plan.md").id == run.id


def test_update_run_context_merges_concurrent_summary_updates(temp_db, monkeypatch) -> None:
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.projects import LocalProjectManager

    project = LocalProjectManager(temp_db).create(
        "build-history-concurrent-context",
        repo_path="/tmp/history-concurrent-context",
    )
    history = BuildHistoryStorage(temp_db)
    run = history.start_run(
        project_id=project.id,
        input_ref="plan.md",
        action="build",
        summary={"initial": True},
    )
    first_history = BuildHistoryStorage(temp_db)
    second_history = BuildHistoryStorage(temp_db)
    first_reads = Barrier(2)

    def synchronize_first_lookup(worker_history):
        original_require_run = worker_history._require_run
        first_call = True

        def gated_require_run(run_id: str):
            nonlocal first_call
            result = original_require_run(run_id)
            if first_call:
                first_call = False
                first_reads.wait(timeout=5)
            return result

        return gated_require_run

    for worker_history in (first_history, second_history):
        monkeypatch.setattr(
            worker_history,
            "_require_run",
            synchronize_first_lookup(worker_history),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        updates = (
            executor.submit(
                first_history.update_run_context,
                run.id,
                summary={"coordinator_session_id": "coordinator"},
            ),
            executor.submit(
                second_history.update_run_context,
                run.id,
                summary={"root_task_id": "task-root"},
            ),
        )
        for update in updates:
            update.result(timeout=10)

    updated = history.get_run(run.id)
    assert updated is not None
    assert updated.summary == {
        "initial": True,
        "coordinator_session_id": "coordinator",
        "root_task_id": "task-root",
    }


def test_build_history_finds_latest_coordinated_ancestor_run(temp_db) -> None:
    """Latest coordinated run lookup should walk task ancestors within the project."""
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create(
        "build-history-coordinator",
        repo_path="/tmp/history-coordinator",
    )
    other_project = LocalProjectManager(temp_db).create(
        "other-build-history-coordinator",
        repo_path="/tmp/other-history-coordinator",
    )
    tasks = LocalTaskManager(temp_db)
    root = tasks.create_task(project.id, "Build root", task_type="epic")
    child = tasks.create_task(
        project.id,
        "Build child",
        parent_task_id=root.id,
        validation_criteria=VALIDATION_CRITERIA,
    )
    leaf = tasks.create_task(
        project.id,
        "Build leaf",
        parent_task_id=child.id,
        validation_criteria=VALIDATION_CRITERIA,
    )
    unrelated = tasks.create_task(project.id, "Unrelated", validation_criteria=VALIDATION_CRITERIA)
    other_project_task = tasks.create_task(
        other_project.id, "Other project", validation_criteria=VALIDATION_CRITERIA
    )
    history = BuildHistoryStorage(temp_db)

    root_run = history.record_run(
        project_id=project.id,
        root_task_id=root.id,
        input_ref=f"#{root.seq_num}",
        action="build",
        summary={"coordinator_session_id": "coord-root"},
    )
    child_run = history.record_run(
        project_id=project.id,
        root_task_id=child.id,
        input_ref=f"#{child.seq_num}",
        action="build",
        summary={"coordinator_session_id": "coord-child"},
    )
    uncoordinated_leaf_run = history.record_run(
        project_id=project.id,
        root_task_id=leaf.id,
        input_ref=f"#{leaf.seq_num}",
        action="build",
        summary={"quick": True},
    )
    unrelated_run = history.record_run(
        project_id=project.id,
        root_task_id=unrelated.id,
        input_ref=f"#{unrelated.seq_num}",
        action="build",
        summary={"coordinator_session_id": "coord-unrelated"},
    )
    other_project_run = history.record_run(
        project_id=other_project.id,
        root_task_id=other_project_task.id,
        input_ref=f"#{other_project_task.seq_num}",
        action="build",
        summary={"coordinator_session_id": "coord-other-project"},
    )
    for run, started_at in [
        (root_run, "2026-01-01T00:00:00+00:00"),
        (child_run, "2026-01-03T00:00:00+00:00"),
        (uncoordinated_leaf_run, "2026-01-04T00:00:00+00:00"),
        (unrelated_run, "2026-01-05T00:00:00+00:00"),
        (other_project_run, "2026-01-06T00:00:00+00:00"),
    ]:
        temp_db.execute("UPDATE build_runs SET started_at = %s WHERE id = %s", (started_at, run.id))

    latest = history.latest_coordinated_run_for_task(project.id, leaf.id)

    assert latest is not None
    assert latest.id == child_run.id
    assert latest.summary["coordinator_session_id"] == "coord-child"
