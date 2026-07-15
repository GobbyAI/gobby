from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.storage.cron import CronJobStorage
from gobby.storage.projects import LocalProjectManager
from gobby.wiki.scheduled_jobs import (
    WIKI_RECAP_SCHEDULE_CRON,
    WIKI_RECAP_TIMEOUT_SECONDS,
    register_wiki_cron_jobs_for_projects,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_startup_reconciles_installed_recap_timeout(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
    enabled: bool,
) -> None:
    scope = f"project:{project_id}"
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
        enabled=enabled,
        is_system=True,
    )
    refresh = cron_storage.create_job(
        project_id=project_id,
        name=wiki_job_name("refresh", scope),
        description="existing refresh definition",
        schedule_type="interval",
        interval_seconds=300,
        action_type="handler",
        action_config={
            "handler": wiki_handler_name("refresh", scope),
            "scope": scope,
            "command": "refresh",
        },
        enabled=False,
        is_system=True,
    )

    await register_wiki_cron_jobs_for_projects(
        cron_storage=cron_storage,
        cron_executor=MagicMock(),
        project_scopes=((project_id, []),),
        db=temp_db,
        gateway_factory=MagicMock(return_value=MagicMock()),
    )

    recap = cron_storage.get_job_by_name(recap_name)
    persisted_refresh = cron_storage.get_job(refresh.id)
    assert recap is not None
    assert recap.id == existing.id
    assert recap.enabled is enabled
    assert recap.action_config["timeout_seconds"] == WIKI_RECAP_TIMEOUT_SECONDS == 3600
    assert persisted_refresh is not None
    assert "timeout_seconds" not in persisted_refresh.action_config
