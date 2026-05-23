"""Red tests for system-managed cron row behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: Any) -> CronJobStorage:
    return CronJobStorage(temp_db)


def _job(storage: CronJobStorage, *, is_system: bool) -> CronJob:
    job = storage.create_job(
        project_id=PROJECT_ID,
        name="dispatcher" if is_system else "operator",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
    )
    if is_system:
        storage.mark_as_system_job(job.id)
    stored = storage.get_job(job.id)
    assert stored is not None
    return stored


def test_list_jobs_filters_by_is_system(cron_storage: CronJobStorage) -> None:
    system = _job(cron_storage, is_system=True)
    _job(cron_storage, is_system=False)

    jobs = cron_storage.list_jobs(project_id=PROJECT_ID, is_system=True)

    assert [job.id for job in jobs] == [system.id]


def test_mark_as_system_job_sets_system_flag(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=False)

    cron_storage.mark_as_system_job(job.id)

    updated = cron_storage.get_job(job.id)
    assert updated is not None
    assert updated.is_system is True


def test_delete_refuses_system_row(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    with pytest.raises(ValueError, match="system-managed.*delete"):
        cron_storage.delete_job(job.id)


def test_enabled_update_allowed_on_system_row(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    updated = cron_storage.update_job(job.id, enabled=False)

    assert updated is not None
    assert updated.enabled is False


def test_schedule_field_updates_allowed_on_system_row(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    updated = cron_storage.update_job(
        job.id,
        schedule_type="cron",
        cron_expr="*/5 * * * *",
        interval_seconds=None,
        run_at=None,
        timezone="America/Chicago",
    )

    assert updated is not None
    assert updated.schedule_type == "cron"
    assert updated.cron_expr == "*/5 * * * *"
    assert updated.timezone == "America/Chicago"


@pytest.mark.parametrize(
    "field,value", [("name", "renamed"), ("action_type", "shell"), ("action_config", {"x": 1})]
)
def test_system_row_definition_updates_refused(
    cron_storage: CronJobStorage, field: str, value: Any
) -> None:
    job = _job(cron_storage, is_system=True)

    with pytest.raises(ValueError, match=rf"system-managed.*{field}"):
        cron_storage.update_job(job.id, **{field: value})


def test_bookkeeping_fields_refused_via_operator_surface(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    with pytest.raises(ValueError, match="system-managed.*next_run_at"):
        cron_storage.update_job(job.id, next_run_at=datetime.now(UTC).isoformat())


def test_protection_error_message_names_system_and_field(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    with pytest.raises(ValueError, match="system-managed.*action_config"):
        cron_storage.update_job(job.id, action_config={"handler": "operator"})


def test_bookkeeping_refuses_non_system_row(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=False)

    with pytest.raises(ValueError, match="non-system"):
        cron_storage.update_system_job_bookkeeping(job.id, next_run_at=None)


def test_bookkeeping_rejects_non_bookkeeping_fields(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    with pytest.raises(ValueError, match="name"):
        cron_storage.update_system_job_bookkeeping(job.id, name="bad")


def test_bookkeeping_partial_update_preserves_telemetry(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    cron_storage.update_system_job_bookkeeping(
        job.id,
        last_run_at="2026-04-30T00:00:00Z",
        last_status="completed",
        consecutive_failures=2,
    )
    updated = cron_storage.update_system_job_bookkeeping(
        job.id,
        next_run_at="2026-05-01T00:00:00Z",
    )

    assert updated is not None
    assert updated.next_run_at == "2026-05-01T00:00:00+00:00"
    assert updated.last_run_at == "2026-04-30T00:00:00+00:00"
    assert updated.last_status == "completed"
    assert updated.consecutive_failures == 2


def test_bookkeeping_telemetry_update_preserves_next_run_at(
    cron_storage: CronJobStorage,
) -> None:
    job = _job(cron_storage, is_system=True)

    cron_storage.update_system_job_bookkeeping(job.id, next_run_at="2026-05-01T00:00:00Z")
    updated = cron_storage.update_system_job_bookkeeping(
        job.id,
        last_run_at="2026-04-30T00:00:00Z",
        last_status="completed",
        consecutive_failures=0,
    )

    assert updated is not None
    assert updated.next_run_at == "2026-05-01T00:00:00+00:00"
    assert updated.last_run_at == "2026-04-30T00:00:00+00:00"
    assert updated.last_status == "completed"
    assert updated.consecutive_failures == 0


def test_bookkeeping_explicit_none_writes_null_without_clobbering_others(
    cron_storage: CronJobStorage,
) -> None:
    job = _job(cron_storage, is_system=True)

    cron_storage.update_system_job_bookkeeping(
        job.id,
        next_run_at="2026-05-01T00:00:00Z",
        last_status="completed",
    )
    updated = cron_storage.update_system_job_bookkeeping(job.id, next_run_at=None)

    assert updated is not None
    assert updated.next_run_at is None
    assert updated.last_status == "completed"


def test_reconcile_refuses_non_system_row(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=False)

    with pytest.raises(ValueError, match="non-system"):
        cron_storage.reconcile_system_job_definition(
            job.id, action_type="handler", action_config={}
        )


def test_reconcile_no_op_when_action_in_sync(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)

    updated = cron_storage.reconcile_system_job_definition(
        job.id,
        action_type=job.action_type,
        action_config=job.action_config,
    )

    assert updated is not None
    assert updated.updated_at == job.updated_at


def test_reconcile_does_not_overwrite_schedule_fields(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)
    cron_storage.update_job(job.id, interval_seconds=300)

    updated = cron_storage.reconcile_system_job_definition(
        job.id,
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
    )

    assert updated is not None
    assert updated.interval_seconds == 300


def test_toggle_job_on_system_row_recomputes_next_run_at(cron_storage: CronJobStorage) -> None:
    job = _job(cron_storage, is_system=True)
    cron_storage.update_job(job.id, enabled=False)
    cron_storage.update_system_job_bookkeeping(job.id, next_run_at=None)

    updated = cron_storage.toggle_job(job.id)

    assert updated is not None
    assert updated.enabled is True
    assert updated.next_run_at is not None


def test_system_row_constants_and_sentinel_exist() -> None:
    from gobby.storage.cron import SYSTEM_ROW_UPDATE_ALLOWED_FIELDS, UNSET

    assert SYSTEM_ROW_UPDATE_ALLOWED_FIELDS == {
        "enabled",
        "schedule_type",
        "cron_expr",
        "interval_seconds",
        "run_at",
        "timezone",
    }
    assert repr(UNSET) == "UNSET"
