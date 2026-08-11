"""Dormant CodeWiki state reconciliation.

Generated-content maintenance paused while the wiki redesign is pending.
"""

from __future__ import annotations

from dataclasses import dataclass

from gobby.storage.cron import CODEWIKI_NIGHTLY_JOB_PREFIX, CronJobStorage

CODEWIKI_DISABLED_REASON = "pending_wiki_redesign"

__all__ = [
    "CODEWIKI_DISABLED_REASON",
    "CodewikiCronReconciliation",
    "reconcile_codewiki_crons_disabled",
]


@dataclass(frozen=True)
class CodewikiCronReconciliation:
    """Outcome of disabling persisted CodeWiki nightly cron rows."""

    disabled: tuple[str, ...]
    failed: tuple[str, ...]
    residual_enabled: tuple[str, ...]


def reconcile_codewiki_crons_disabled(
    cron_storage: CronJobStorage,
) -> CodewikiCronReconciliation:
    """Idempotently disable every enabled CodeWiki nightly cron row."""
    disabled: list[str] = []
    failed: list[str] = []

    jobs = cron_storage.list_jobs_by_name_prefix(CODEWIKI_NIGHTLY_JOB_PREFIX, enabled=True)
    for job in jobs:
        try:
            updated = cron_storage.update_job(job.id, enabled=False)
        except Exception:
            failed.append(job.id)
            continue
        if updated is None:
            failed.append(job.id)
        else:
            disabled.append(job.id)

    residual = cron_storage.list_jobs_by_name_prefix(CODEWIKI_NIGHTLY_JOB_PREFIX, enabled=True)
    return CodewikiCronReconciliation(
        disabled=tuple(disabled),
        failed=tuple(failed),
        residual_enabled=tuple(job.id for job in residual),
    )
