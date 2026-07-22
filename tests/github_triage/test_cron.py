from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.github_triage.cron import (
    github_triage_handler_name,
    github_triage_job_name,
    register_github_triage_cron,
)
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _register(temp_db, sample_project):
    storage = CronJobStorage(temp_db)
    executor = CronExecutor(storage=storage)
    count = register_github_triage_cron(
        cron_storage=storage,
        cron_executor=executor,
        db=temp_db,
        mcp_manager=MagicMock(),
        task_manager=LocalTaskManager(temp_db),
        project_id=sample_project["id"],
    )
    return storage, executor, count


def test_register_github_triage_cron_creates_project_system_job(
    temp_db,
    sample_project,
) -> None:
    GitHubTriageStore(temp_db).upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            sync_enabled=True,
            triage_enabled=True,
            repositories=("owner/repo",),
            reconcile_interval_seconds=1200,
        )
    )

    storage, executor, count = _register(temp_db, sample_project)

    job = storage.get_job_by_name(github_triage_job_name(sample_project["id"]))
    assert count == 1
    assert job is not None
    assert job.is_system is True
    assert job.action_type == "handler"
    assert job.action_config == {"handler": github_triage_handler_name(sample_project["id"])}
    assert job.interval_seconds == 1200
    assert executor.has_handler(github_triage_handler_name(sample_project["id"]))


def test_register_github_triage_cron_disables_existing_job_when_config_disabled(
    temp_db,
    sample_project,
) -> None:
    store = GitHubTriageStore(temp_db)
    store.upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            sync_enabled=True,
            triage_enabled=True,
            repositories=("owner/repo",),
        )
    )
    storage, _executor, _count = _register(temp_db, sample_project)
    job = storage.get_job_by_name(github_triage_job_name(sample_project["id"]))
    assert job is not None
    assert job.enabled is True

    store.upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            sync_enabled=False,
            triage_enabled=False,
            repositories=("owner/repo",),
        )
    )
    storage, _executor, count = _register(temp_db, sample_project)

    job = storage.get_job_by_name(github_triage_job_name(sample_project["id"]))
    assert count == 0
    assert job is not None
    assert job.enabled is False


def test_handler_registration_failure_does_not_create_enabled_job(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GitHubTriageStore(temp_db).upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            sync_enabled=True,
            triage_enabled=True,
            repositories=("owner/repo",),
        )
    )
    storage = CronJobStorage(temp_db)
    executor = CronExecutor(storage=storage)
    monkeypatch.setattr(
        executor,
        "register_handler",
        MagicMock(side_effect=RuntimeError("handler registration failed")),
    )

    count = register_github_triage_cron(
        cron_storage=storage,
        cron_executor=executor,
        db=temp_db,
        mcp_manager=MagicMock(),
        task_manager=LocalTaskManager(temp_db),
        project_id=sample_project["id"],
    )

    assert count == 0
    assert storage.get_job_by_name(github_triage_job_name(sample_project["id"])) is None


def test_handler_registration_failure_disables_existing_enabled_job(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GitHubTriageStore(temp_db).upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            sync_enabled=True,
            triage_enabled=True,
            repositories=("owner/repo",),
        )
    )
    storage, _executor, _count = _register(temp_db, sample_project)
    existing = storage.get_job_by_name(github_triage_job_name(sample_project["id"]))
    assert existing is not None
    assert existing.enabled is True

    executor = CronExecutor(storage=storage)
    monkeypatch.setattr(
        executor,
        "register_handler",
        MagicMock(side_effect=RuntimeError("handler registration failed")),
    )
    count = register_github_triage_cron(
        cron_storage=storage,
        cron_executor=executor,
        db=temp_db,
        mcp_manager=MagicMock(),
        task_manager=LocalTaskManager(temp_db),
        project_id=sample_project["id"],
    )

    job = storage.get_job_by_name(github_triage_job_name(sample_project["id"]))
    assert count == 0
    assert job is not None
    assert job.enabled is False


def test_registration_failure_for_one_project_does_not_abort_later_projects(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_manager = LocalProjectManager(temp_db)
    later_project = project_manager.create("later-triage-project", repo_path="/tmp/later")
    store = GitHubTriageStore(temp_db)
    for project_id in (sample_project["id"], later_project.id):
        store.upsert_config(
            GitHubTriageConfig(
                project_id=project_id,
                sync_enabled=True,
                triage_enabled=True,
                repositories=("owner/repo",),
            )
        )
    storage = CronJobStorage(temp_db)
    executor = CronExecutor(storage=storage)
    original_create_job = storage.create_job

    def flaky_create_job(**kwargs):
        if kwargs["project_id"] == sample_project["id"]:
            raise RuntimeError("first project registration failed")
        return original_create_job(**kwargs)

    monkeypatch.setattr(storage, "create_job", flaky_create_job)

    count = register_github_triage_cron(
        cron_storage=storage,
        cron_executor=executor,
        db=temp_db,
        mcp_manager=MagicMock(),
        task_manager=LocalTaskManager(temp_db),
        project_manager=project_manager,
    )

    assert count == 1
    assert storage.get_job_by_name(github_triage_job_name(later_project.id)) is not None
    assert executor.has_handler(github_triage_handler_name(later_project.id))
