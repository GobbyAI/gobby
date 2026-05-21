from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


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


def test_build_history_start_and_finish_updates_root_and_status(temp_db) -> None:
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("build-history-finish", repo_path="/tmp/history")
    task = LocalTaskManager(temp_db).create_task(project.id, "Build root")
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
