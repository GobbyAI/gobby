"""Machine ownership boundaries for cron scheduler reconciliation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import gobby.scheduler.scheduler as scheduler_module
import gobby.storage.cron_runs as cron_runs_module
from gobby.config.cron import CronConfig
from gobby.scheduler.scheduler import CronScheduler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronRun
from tests.config_runtime_helpers import static_cron_capture
from tests.fixtures.postgres import TEST_USER_ID

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


def _seed_active_runs(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CronJobStorage, str, CronRun, CronRun]:
    local_machine_id = str(uuid.uuid4())
    remote_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, remote_machine_id):
        temp_db.execute(
            "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s)",
            (machine_id, f"host-{machine_id}", TEST_USER_ID),
        )

    storage = CronJobStorage(temp_db)
    jobs = [
        storage.create_job(
            project_id=PROJECT_ID,
            name=name,
            schedule_type="cron",
            action_type="handler",
            action_config={"handler": name},
            cron_expr="0 * * * *",
        )
        for name in ("local-machine-run", "remote-machine-run")
    ]

    monkeypatch.setattr(cron_runs_module, "get_machine_id", lambda: local_machine_id)
    local_run = storage.create_run(jobs[0].id)
    monkeypatch.setattr(cron_runs_module, "get_machine_id", lambda: remote_machine_id)
    remote_run = storage.create_run(jobs[1].id)
    assert local_run is not None
    assert remote_run is not None

    stale_started_at = datetime.now(UTC) - timedelta(minutes=5)
    for run in (local_run, remote_run):
        storage.update_run(run.id, status="running", started_at=stale_started_at)
    return storage, local_machine_id, local_run, remote_run


def test_restart_does_not_fail_remote_runs(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, local_machine_id, local_run, remote_run = _seed_active_runs(
        temp_db,
        monkeypatch,
    )
    monkeypatch.setattr(
        scheduler_module,
        "require_machine_id",
        lambda: local_machine_id,
        raising=False,
    )
    scheduler = CronScheduler(
        storage=storage,
        executor=MagicMock(),
        capture_bundle=static_cron_capture(CronConfig()),
    )

    scheduler._reconcile_interrupted_runs_on_startup()

    refreshed_local = storage.get_run(local_run.id)
    refreshed_remote = storage.get_run(remote_run.id)
    assert refreshed_local is not None
    assert refreshed_local.status == "failed"
    assert refreshed_remote is not None
    assert refreshed_remote.status == "running"


def test_stale_sweep_and_slots_scoped(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, local_machine_id, local_run, remote_run = _seed_active_runs(
        temp_db,
        monkeypatch,
    )

    assert storage.count_running(local_machine_id) == 1
    failed = storage.fail_stale_running_runs(60, machine_id=local_machine_id)

    refreshed_local = storage.get_run(local_run.id)
    refreshed_remote = storage.get_run(remote_run.id)
    assert failed == 1
    assert refreshed_local is not None
    assert refreshed_local.status == "failed"
    assert refreshed_remote is not None
    assert refreshed_remote.status == "running"

    admitted_job = storage.create_job(
        project_id=PROJECT_ID,
        name="local-admission-with-remote-capacity",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "local-admission"},
        cron_expr="0 * * * *",
    )
    admitted_run, active_count, already_running = storage.create_run_if_admitted(
        admitted_job.id,
        machine_id=local_machine_id,
        max_concurrent_jobs=1,
    )
    assert admitted_run is not None
    assert admitted_run.machine_id == local_machine_id
    assert active_count == 0
    assert already_running is False
