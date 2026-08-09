"""Tests for disabling persisted CodeWiki cron state."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.wiki.codewiki_dormant import (
    CODEWIKI_NIGHTLY_JOB_PREFIX,
    GENERATED_CONTENT_MAINTENANCE_STATE,
    reconcile_codewiki_crons_disabled,
)
from gobby.wiki.scheduled_jobs import _wiki_command_specs

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: Any) -> CronJobStorage:
    return CronJobStorage(temp_db)


def _nightly_job(
    storage: CronJobStorage,
    suffix: str,
    *,
    is_system: bool = True,
) -> CronJob:
    return storage.create_job(
        project_id=PROJECT_ID,
        name=f"{CODEWIKI_NIGHTLY_JOB_PREFIX}{suffix}",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": f"codewiki.nightly.{suffix}"},
        interval_seconds=60,
        is_system=is_system,
    )


def test_reconcile_disables_and_preserves(cron_storage: CronJobStorage) -> None:
    system = _nightly_job(cron_storage, "project:alpha")
    legacy = _nightly_job(cron_storage, "project:legacy", is_system=False)
    other_wiki = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:wiki-upkeep:project:alpha",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "wiki:upkeep:project:alpha"},
        interval_seconds=60,
        is_system=True,
    )

    result = reconcile_codewiki_crons_disabled(cron_storage)

    assert result.disabled == (system.id, legacy.id)
    assert result.failed == ()
    assert result.residual_enabled == ()
    for job_id in (system.id, legacy.id):
        stored = cron_storage.get_job(job_id)
        assert stored is not None
        assert stored.enabled is False
        assert stored.next_run_at is None
    assert cron_storage.get_job(other_wiki.id) == other_wiki
    assert reconcile_codewiki_crons_disabled(cron_storage).disabled == ()


@pytest.mark.parametrize("failure_mode", ["raise", "none"])
def test_mid_loop_failure_degrades_and_converges(
    cron_storage: CronJobStorage,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    first = _nightly_job(cron_storage, "project:alpha")
    failed = _nightly_job(cron_storage, "project:beta")
    last = _nightly_job(cron_storage, "project:gamma", is_system=False)
    update_job = cron_storage.update_job

    def fail_one(job_id: str, **fields: Any) -> CronJob | None:
        if job_id == failed.id:
            if failure_mode == "raise":
                raise RuntimeError("injected update failure")
            return None
        return update_job(job_id, **fields)

    monkeypatch.setattr(cron_storage, "update_job", fail_one)

    result = reconcile_codewiki_crons_disabled(cron_storage)

    assert result.disabled == (first.id, last.id)
    assert result.failed == (failed.id,)
    assert result.residual_enabled == (failed.id,)

    monkeypatch.setattr(cron_storage, "update_job", update_job)
    converged = reconcile_codewiki_crons_disabled(cron_storage)
    assert converged.disabled == (failed.id,)
    assert converged.failed == ()
    assert converged.residual_enabled == ()


def test_legacy_non_system_row_reconciled(
    cron_storage: CronJobStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _nightly_job(cron_storage, "project:legacy", is_system=False)
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    cron_storage.update_job(legacy.id, next_run_at=due_at)
    assert legacy.id in {job.id for job in cron_storage.get_due_jobs()}
    update_job = cron_storage.update_job

    def fail_legacy(job_id: str, **fields: Any) -> CronJob | None:
        if job_id == legacy.id:
            return None
        return update_job(job_id, **fields)

    monkeypatch.setattr(cron_storage, "update_job", fail_legacy)
    failed = reconcile_codewiki_crons_disabled(cron_storage)
    assert failed.failed == (legacy.id,)
    assert failed.residual_enabled == (legacy.id,)

    monkeypatch.setattr(cron_storage, "update_job", update_job)
    reconciled = reconcile_codewiki_crons_disabled(cron_storage)
    assert reconciled.disabled == (legacy.id,)
    assert legacy.id not in {job.id for job in cron_storage.get_due_jobs()}


def test_scheduled_wiki_jobs_exclude_generated_code_maintenance() -> None:
    specs = _wiki_command_specs(
        gateway=MagicMock(),
        coordinator=MagicMock(),
        scope="project:alpha",
        task_manager=None,
        fallback_project_id=PROJECT_ID,
    )

    assert GENERATED_CONTENT_MAINTENANCE_STATE == "generated-content maintenance paused"
    assert all("code" not in command for command, *_rest in specs)
