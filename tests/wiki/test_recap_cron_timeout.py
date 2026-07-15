from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.cron import CronJobStorage
from gobby.storage.projects import LocalProjectManager
from gobby.wiki.scheduled_jobs import (
    WIKI_RECAP_SCHEDULE_CRON,
    WIKI_RECAP_TIMEOUT_SECONDS,
    _ensure_wiki_cron_job,
    wiki_handler_name,
    wiki_job_name,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def project_id(temp_db: Any) -> str:
    return LocalProjectManager(temp_db).create(name="wiki", repo_path="/tmp/wiki").id


@pytest.fixture
def cron_storage(temp_db: Any) -> CronJobStorage:
    return CronJobStorage(temp_db)


def test_startup_reconciles_recap_timeout_without_affecting_other_wiki_jobs(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    scope = "project:alpha"
    recap_name = wiki_job_name("recap", scope)
    existing = cron_storage.create_job(
        project_id=project_id,
        name=recap_name,
        description="old recap definition",
        schedule_type="cron",
        cron_expr=WIKI_RECAP_SCHEDULE_CRON,
        action_type="handler",
        action_config={
            "handler": wiki_handler_name("recap", scope),
            "scope": scope,
            "command": "recap",
        },
        enabled=True,
        is_system=True,
    )

    _ensure_wiki_cron_job(
        cron_storage=cron_storage,
        project_id=project_id,
        command="recap",
        scope=scope,
        handler_name=wiki_handler_name("recap", scope),
        purpose="Nightly wiki session recap",
        cron_expr=WIKI_RECAP_SCHEDULE_CRON,
    )
    _ensure_wiki_cron_job(
        cron_storage=cron_storage,
        project_id=project_id,
        command="refresh",
        scope=scope,
        handler_name=wiki_handler_name("refresh", scope),
        purpose="Scheduled wiki source refresh",
        interval_seconds=300,
    )

    recap = cron_storage.get_job_by_name(recap_name)
    refresh = cron_storage.get_job_by_name(wiki_job_name("refresh", scope))
    assert recap is not None
    assert recap.id == existing.id
    assert recap.action_config["timeout_seconds"] == WIKI_RECAP_TIMEOUT_SECONDS == 3600
    assert refresh is not None
    assert "timeout_seconds" not in refresh.action_config
