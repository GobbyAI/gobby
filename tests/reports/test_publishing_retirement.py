"""Retired publishing never admits work, even with historical request state."""

from dataclasses import asdict
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.cron import CronConfig
from gobby.feedback.storage import FeedbackReviewStore
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.runner_init.orchestration import RETIRED_SYSTEM_CRON_JOBS
from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronRunRejected, CronScheduler
from gobby.storage.cron import CronJobStorage, SystemRowProtected
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from tests.config_runtime_helpers import static_cron_capture


def _work_counts(db: HubDatabase) -> dict[str, int]:
    counts = {}
    for table in ("tasks", "agent_runs", "worktrees", "synthesis_report_attempts"):
        row = db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
        assert row is not None
        counts[table] = int(row["n"])
    return counts


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "interrupted"])
def test_terminal_sources_leave_publication_history_inactive(
    temp_db: HubDatabase,
    status: str,
) -> None:
    feedback = FeedbackReviewStore(temp_db)
    dream = MemoryDreamStore(temp_db)
    feedback_id = feedback.create_run(
        dry_run=False,
        window_start=None,
        window_end=None,
        rows_considered=0,
    )
    dream_id = dream.create_run(project_id=PERSONAL_PROJECT_ID, dry_run=False, options={})
    temp_db.execute(
        "INSERT INTO synthesis_reports (source_kind, source_run_id, project_id, report_path) "
        "VALUES ('dream', %s, %s, 'retained.md')",
        (dream_id, PERSONAL_PROJECT_ID),
    )
    before = temp_db.fetchall("SELECT * FROM synthesis_reports")
    counts_before = _work_counts(temp_db)
    feedback.save_progress(feedback_id, {}, {"report_project_id": PERSONAL_PROJECT_ID})
    dream.update_run(dream_id, summary={"report_project_id": PERSONAL_PROJECT_ID})
    feedback.finalize_run(feedback_id, status=status, digest_md="Reviewer notes preserved.")
    dream.update_run(dream_id, status=status)
    feedback.mark_running_interrupted()
    dream.mark_interrupted_runs()
    assert temp_db.fetchall("SELECT * FROM synthesis_reports") == before
    assert _work_counts(temp_db) == counts_before
    run = feedback.get_run(feedback_id)
    assert run is not None
    assert run.digest_md == "Reviewer notes preserved."
    assert "publication" not in asdict(run)


async def test_retired_cron_keeps_history_and_rejects_manual_dispatch(temp_db: HubDatabase) -> None:
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id=PERSONAL_PROJECT_ID,
        name="synthesis-reports",
        schedule_type="interval",
        interval_seconds=60,
        action_type="handler",
        action_config={"handler": "synthesis-reports"},
        enabled=True,
        is_system=True,
    )
    assert job.name in RETIRED_SYSTEM_CRON_JOBS
    storage.update_job(job.id, enabled=False)
    storage.delete_removed_automation_jobs()
    retained = storage.get_job(job.id)
    assert retained is not None and retained.enabled is False
    with pytest.raises(SystemRowProtected, match="retired automation"):
        storage.update_job(job.id, enabled=True)
    assert job.id not in {row.id for row in storage.list_jobs(exclude_removed_automation=True)}
    executor = MagicMock(spec=CronExecutor)
    executor.execute = AsyncMock()
    scheduler = CronScheduler(
        storage=storage,
        executor=cast(CronExecutor, executor),
        capture_bundle=static_cron_capture(CronConfig()),
    )
    with pytest.raises(CronRunRejected, match="retired automation"):
        await scheduler.run_now(job.id)
    assert storage.list_runs(job.id) == []
    executor.execute.assert_not_called()
