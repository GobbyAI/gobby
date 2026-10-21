"""Retired cron job identities and scheduling priority policy."""

from gobby.storage.cron_models import CronJob

REMOVED_AUTOMATION_JOB_NAMES = frozenset(
    {"gobby:dispatcher", "gobby:pipeline-heartbeat", "synthesis-reports"}
)
CODEWIKI_NIGHTLY_JOB_PREFIX = "gobby:codewiki-nightly:"
# Retired automation rows must never list, dispatch, or re-enable.
RETIRED_AUTOMATION_JOB_NAME_PREFIXES = (CODEWIKI_NIGHTLY_JOB_PREFIX,)
CRON_JOB_NAME_PRIORITIES = {
    "gobby:pipeline-heartbeat": 0,
    "gobby:code-index-prune": 3,
}
DEFAULT_CRON_JOB_PRIORITY = 2


def cron_job_priority(job: CronJob) -> int:
    return CRON_JOB_NAME_PRIORITIES.get(job.name, DEFAULT_CRON_JOB_PRIORITY)


def is_removed_automation_job(job: CronJob) -> bool:
    return job.name in REMOVED_AUTOMATION_JOB_NAMES or job.name.startswith(
        RETIRED_AUTOMATION_JOB_NAME_PREFIXES
    )
