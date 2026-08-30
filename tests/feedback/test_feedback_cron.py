"""Registration contract for the session-feedback review system cron job."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.config.sessions import FeedbackReviewConfig
from gobby.feedback.cron import (
    FEEDBACK_REVIEW_CRON_HANDLER,
    FEEDBACK_REVIEW_CRON_JOB_NAME,
    register_feedback_review_cron,
)
from gobby.feedback.service import FeedbackReviewService
from gobby.storage.cron import CronJobStorage

pytestmark = pytest.mark.unit


class _FakeCronStorage:
    def __init__(
        self,
        *,
        existing: Any | None = None,
        repaired: Any | None = None,
        update_result: Any | None = None,
    ) -> None:
        self.existing = existing
        self.repaired = repaired
        self.update_result = update_result
        self.created_jobs: list[dict[str, Any]] = []
        self.updated_jobs: list[tuple[str, dict[str, Any]]] = []
        self.reconciled_jobs: list[tuple[str, dict[str, Any]]] = []
        self.system_job_ids: list[str] = []
        self.woken_job_ids: list[str] = []

    def get_job_by_name(self, _name: str) -> Any | None:
        return self.existing

    def create_job(self, **kwargs: Any) -> Any:
        self.created_jobs.append(kwargs)
        return SimpleNamespace(id="created-job", **kwargs)

    def update_job(self, job_id: str, **kwargs: Any) -> Any | None:
        self.updated_jobs.append((job_id, kwargs))
        return self.update_result

    def mark_as_system_job(self, job_id: str) -> None:
        self.system_job_ids.append(job_id)

    def reconcile_system_job_definition(self, job_id: str, **kwargs: Any) -> Any | None:
        self.reconciled_jobs.append((job_id, kwargs))
        return self.repaired

    def wake_system_job(self, job_id: str) -> Any:
        self.woken_job_ids.append(job_id)
        return SimpleNamespace(id=job_id, enabled=True)


class _FakeCronExecutor:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


class _FakeReviewService:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.dry_runs: list[bool] = []

    async def run_review(self, *, dry_run: bool = False) -> dict[str, Any]:
        self.dry_runs.append(dry_run)
        return self.result


def _stub_service() -> FeedbackReviewService:
    """Registration-only service stand-in; the handler is never invoked."""
    return cast(FeedbackReviewService, MagicMock())


def test_register_feedback_review_cron_creates_single_system_job() -> None:
    cron_storage = _FakeCronStorage()
    cron_executor = _FakeCronExecutor()

    registered = register_feedback_review_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        service=_stub_service(),
        config=FeedbackReviewConfig(enabled=True),
        project_id="proj-1",
    )

    assert registered == 1
    assert set(cron_executor.handlers) == {FEEDBACK_REVIEW_CRON_HANDLER}
    assert len(cron_storage.created_jobs) == 1
    kwargs = cron_storage.created_jobs[0]
    assert kwargs["name"] == FEEDBACK_REVIEW_CRON_JOB_NAME
    assert kwargs["project_id"] == "proj-1"
    assert kwargs["schedule_type"] == "cron"
    # Default nightly slot after dream (02:00) and recap (00:10).
    assert kwargs["cron_expr"] == "0 3 * * *"
    assert kwargs["action_config"] == {
        "handler": FEEDBACK_REVIEW_CRON_HANDLER,
        "timeout_seconds": 1800.0,
        "restart_protected": False,
    }
    assert kwargs["is_system"] is True
    assert kwargs["enabled"] is True


def test_register_feedback_review_cron_disable_path_disables_existing_job() -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=True),
        update_result=SimpleNamespace(id="job-1", enabled=False),
    )
    cron_executor = _FakeCronExecutor()

    registered = register_feedback_review_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=cron_executor,
        service=_stub_service(),
        config=FeedbackReviewConfig(enabled=False),
        project_id="proj-1",
    )

    assert registered == 0
    assert cron_executor.handlers == {}
    assert cron_storage.created_jobs == []
    assert cron_storage.updated_jobs == [("job-1", {"enabled": False, "next_run_at": None})]


def test_register_feedback_review_cron_tolerates_missing_job_during_disable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=True),
        update_result=None,
    )

    registered = register_feedback_review_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=_FakeCronExecutor(),
        service=_stub_service(),
        config=FeedbackReviewConfig(enabled=False),
        project_id="proj-1",
    )

    assert registered == 0
    assert "already disappeared during disable" in caplog.text


def test_register_feedback_review_cron_repairs_existing_job_and_marks_system() -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=False, is_system=False),
        repaired=SimpleNamespace(id="job-1", enabled=False),
    )

    register_feedback_review_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=_FakeCronExecutor(),
        service=_stub_service(),
        config=FeedbackReviewConfig(enabled=True, schedule_cron="30 4 * * *"),
        project_id="proj-1",
    )

    assert cron_storage.created_jobs == []
    assert cron_storage.system_job_ids == ["job-1"]
    job_id, reconciled = cron_storage.reconciled_jobs[0]
    assert job_id == "job-1"
    assert reconciled["cron_expr"] == "30 4 * * *"
    assert reconciled["action_config"] == {
        "handler": FEEDBACK_REVIEW_CRON_HANDLER,
        "timeout_seconds": 1800.0,
        "restart_protected": False,
    }
    # The job was deliberately disabled, so reconcile must not wake it.
    assert cron_storage.woken_job_ids == []


def test_register_feedback_review_cron_restores_previously_enabled_system_job() -> None:
    cron_storage = _FakeCronStorage(
        existing=SimpleNamespace(id="job-1", enabled=True, is_system=True),
        repaired=SimpleNamespace(id="job-1", enabled=False),
    )

    register_feedback_review_cron(
        cron_storage=cast(CronJobStorage, cron_storage),
        cron_executor=_FakeCronExecutor(),
        service=_stub_service(),
        config=FeedbackReviewConfig(enabled=True),
        project_id="proj-1",
    )

    assert cron_storage.system_job_ids == []
    assert cron_storage.woken_job_ids == ["job-1"]


async def test_feedback_review_cron_handler_formats_completed_run() -> None:
    cron_executor = _FakeCronExecutor()
    service = _FakeReviewService(
        {
            "status": "completed",
            "run_id": "run-1",
            "rows_considered": 5,
            "tasks_filed": 2,
            "deduplicated": 1,
        }
    )

    register_feedback_review_cron(
        cron_storage=cast(CronJobStorage, _FakeCronStorage()),
        cron_executor=cron_executor,
        service=cast(FeedbackReviewService, service),
        config=FeedbackReviewConfig(enabled=True),
        project_id="proj-1",
    )

    handler = cron_executor.handlers[FEEDBACK_REVIEW_CRON_HANDLER]
    message = await handler(SimpleNamespace(id="job-1"))

    assert message == "feedback review: run run-1, 5 row(s), 2 task(s) filed, 1 deduplicated"
    # The scheduled path always runs for real.
    assert service.dry_runs == [False]


async def test_feedback_review_cron_handler_reports_empty_backlog() -> None:
    cron_executor = _FakeCronExecutor()
    service = _FakeReviewService({"status": "no_rows", "run_id": None, "rows_considered": 0})

    register_feedback_review_cron(
        cron_storage=cast(CronJobStorage, _FakeCronStorage()),
        cron_executor=cron_executor,
        service=cast(FeedbackReviewService, service),
        config=FeedbackReviewConfig(enabled=True),
        project_id="proj-1",
    )

    handler = cron_executor.handlers[FEEDBACK_REVIEW_CRON_HANDLER]
    assert await handler(SimpleNamespace(id="job-1")) == "feedback review: no unreviewed rows"
