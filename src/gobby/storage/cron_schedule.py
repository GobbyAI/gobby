"""Next-run policy for cron, interval and one-shot jobs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from gobby.storage.cron_constants import MIN_CRON_INTERVAL_SECONDS
from gobby.storage.cron_models import CronJob

logger = logging.getLogger(__name__)


def compute_next_run(job: CronJob) -> datetime | None:
    """Compute the next run time for a cron job.

    Args:
        job: CronJob instance

    Returns:
        Next run datetime (UTC) or None if job is disabled or expired one-shot.
    """
    if not job.enabled:
        return None

    try:
        tz = ZoneInfo(job.timezone) if job.timezone else ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        logger.warning("Invalid timezone %r for job %s, falling back to UTC", job.timezone, job.id)
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)

    if job.schedule_type == "cron":
        if not job.cron_expr:
            return None
        try:
            cron = croniter(job.cron_expr, now)
            next_dt: datetime = cron.get_next(datetime)
            return next_dt.astimezone(ZoneInfo("UTC"))
        except (ValueError, KeyError):
            # Invalid cron expression
            return None

    elif job.schedule_type == "interval":
        if not job.interval_seconds:
            return None
        interval_seconds = max(job.interval_seconds, MIN_CRON_INTERVAL_SECONDS)
        # Always compute from now to prevent double-fire when last_run_at
        # is stale (close to current time after execution).
        next_interval: datetime = now + timedelta(seconds=interval_seconds)
        return next_interval.astimezone(ZoneInfo("UTC"))

    elif job.schedule_type == "once":
        if not job.run_at:
            logger.debug("Job %s: schedule_type='once' but run_at is missing", job.id)
            return None
        run_at_utc = job.run_at.astimezone(ZoneInfo("UTC"))
        # Expired one-shot
        now_utc = datetime.now(ZoneInfo("UTC"))
        if run_at_utc <= now_utc:
            logger.debug(
                "Job %s: one-shot run_at %s is in the past (now=%s)",
                job.id,
                run_at_utc,
                now_utc,
            )
            return None
        return run_at_utc

    return None
