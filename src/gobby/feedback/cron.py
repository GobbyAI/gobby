"""System cron registration for the session-feedback review loop."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import PERSONAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.config.sessions import FeedbackReviewConfig
    from gobby.feedback.service import FeedbackReviewService

logger = logging.getLogger(__name__)

FEEDBACK_REVIEW_CRON_JOB_NAME = "gobby:feedback-review"
FEEDBACK_REVIEW_CRON_HANDLER = "feedback.review"
FEEDBACK_REVIEW_CRON_DESCRIPTION = "Scheduled session-feedback review and task filing"
# One distill call plus deterministic task filing; far under the dream sweep.
FEEDBACK_REVIEW_TIMEOUT_SECONDS = 1800.0

CronHandler = Callable[[CronJob], Awaitable[str]]


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


def _action_config() -> dict[str, str | float | bool]:
    # Not restart_protected: rows stay unreviewed until a run completes, so an
    # interrupted run is simply re-picked by the next one.
    return {
        "handler": FEEDBACK_REVIEW_CRON_HANDLER,
        "timeout_seconds": FEEDBACK_REVIEW_TIMEOUT_SECONDS,
        "restart_protected": False,
    }


def register_feedback_review_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    service: FeedbackReviewService,
    config: FeedbackReviewConfig,
    project_id: str | None = None,
) -> int:
    """Register the feedback-review handler and reconcile its single system row."""
    if not config.enabled:
        existing = cron_storage.get_job_by_name(FEEDBACK_REVIEW_CRON_JOB_NAME)
        if existing and existing.enabled:
            updated = cron_storage.update_job(existing.id, enabled=False, next_run_at=None)
            if updated is None:
                logger.warning(
                    "System cron job already disappeared during disable: %s",
                    FEEDBACK_REVIEW_CRON_JOB_NAME,
                )
        return 0

    async def _handler(_job: CronJob) -> str:
        result = await service.run_review(dry_run=False)
        if result["status"] == "no_rows":
            return "feedback review: no unreviewed rows"
        return (
            f"feedback review: run {result['run_id']}, "
            f"{result['rows_considered']} row(s), "
            f"{result['tasks_filed']} task(s) filed, "
            f"{result['deduplicated']} deduplicated"
        )

    cron_executor.register_handler(FEEDBACK_REVIEW_CRON_HANDLER, _handler)
    _ensure_system_job(cron_storage, config, project_id)
    return 1


def _ensure_system_job(
    cron_storage: CronJobStorage,
    config: FeedbackReviewConfig,
    project_id: str | None,
) -> None:
    existing = cron_storage.get_job_by_name(FEEDBACK_REVIEW_CRON_JOB_NAME)
    cron_expr = str(config.schedule_cron)
    target_project_id = project_id or PERSONAL_PROJECT_ID
    if existing is None:
        cron_storage.create_job(
            project_id=target_project_id,
            name=FEEDBACK_REVIEW_CRON_JOB_NAME,
            description=FEEDBACK_REVIEW_CRON_DESCRIPTION,
            schedule_type="cron",
            cron_expr=cron_expr,
            action_type="handler",
            action_config=_action_config(),
            enabled=True,
            is_system=True,
        )
        logger.info("Created system cron job: %s", FEEDBACK_REVIEW_CRON_JOB_NAME)
        return

    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)
    was_enabled = existing.enabled
    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=_action_config(),
        description=FEEDBACK_REVIEW_CRON_DESCRIPTION,
        schedule_type="cron",
        cron_expr=cron_expr,
        interval_seconds=None,
        run_at=None,
    )
    if repaired and was_enabled and not repaired.enabled:
        cron_storage.wake_system_job(repaired.id)
