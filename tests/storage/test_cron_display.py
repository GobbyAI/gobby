"""Tests for generated cron job display names."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.cron_display import (
    default_cron_display_name,
    effective_cron_display_name,
)
from gobby.storage.cron_models import CronJob

pytestmark = pytest.mark.unit

PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"
PROJECT_NAMES = {PROJECT_ID: "gobby"}


def _job(name: str, display_name: str | None = None) -> CronJob:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    return CronJob(
        id="cj-1",
        project_id=PROJECT_ID,
        name=name,
        schedule_type="interval",
        action_type="handler",
        action_config={},
        created_at=now,
        updated_at=now,
        display_name=display_name,
        interval_seconds=60,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (f"gobby:wiki-recap:project:{PROJECT_ID}", "Wiki recap — gobby"),
        (f"gobby:wiki-sync-sessions:project:{PROJECT_ID}", "Wiki sync sessions — gobby"),
        (f"gobby:codewiki-nightly:{PROJECT_ID}", "Codewiki nightly — gobby"),
        (f"gobby:github-triage:{PROJECT_ID}", "GitHub triage — gobby"),
        ("gobby:wiki-prune", "Wiki prune"),
        ("gobby:code-index-nightly-full-reindex", "Code index nightly full reindex"),
        ("gobby:memory-dream", "Memory dream"),
        ("gobby:monitor:17731-17806-hourly", "Monitor tasks #17731–#17806 (hourly)"),
    ],
)
def test_default_display_name_for_system_identifiers(name: str, expected: str) -> None:
    assert default_cron_display_name(name, PROJECT_NAMES) == expected


def test_default_display_name_falls_back_to_uuid_prefix_when_project_unknown() -> None:
    name = f"gobby:wiki-recap:project:{PROJECT_ID}"

    assert default_cron_display_name(name, {}) == "Wiki recap — d45545c5"


def test_default_display_name_returns_none_outside_gobby_namespace() -> None:
    assert default_cron_display_name("nightly-backup", PROJECT_NAMES) is None


def test_effective_display_name_prefers_stored_override() -> None:
    job = _job("gobby:wiki-prune", display_name="My prune job")

    assert effective_cron_display_name(job, PROJECT_NAMES) == "My prune job"


def test_effective_display_name_ignores_whitespace_override() -> None:
    job = _job("gobby:wiki-prune", display_name="   ")

    assert effective_cron_display_name(job, PROJECT_NAMES) == "Wiki prune"


def test_effective_display_name_uses_raw_name_for_user_jobs() -> None:
    job = _job("nightly-backup")

    assert effective_cron_display_name(job, PROJECT_NAMES) == "nightly-backup"
