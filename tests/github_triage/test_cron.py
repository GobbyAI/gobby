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
            enabled=True,
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
            enabled=True,
            repositories=("owner/repo",),
        )
    )
    storage, _executor, _count = _register(temp_db, sample_project)
    assert storage.get_job_by_name(github_triage_job_name(sample_project["id"])).enabled is True

    store.upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            enabled=False,
            repositories=("owner/repo",),
        )
    )
    storage, _executor, count = _register(temp_db, sample_project)

    job = storage.get_job_by_name(github_triage_job_name(sample_project["id"]))
    assert count == 0
    assert job is not None
    assert job.enabled is False
