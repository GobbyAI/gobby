from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.cron_models import CronJob
from gobby.storage.memories_models import Memory
from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit


def test_task_from_row_normalizes_timestamptz_fields_and_preserves_dates() -> None:
    task = Task.from_row(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "title": "Datetime task",
            "priority": 2,
            "task_type": "refactor",
            "created_at": "2026-01-02T03:04:05",
            "updated_at": datetime(2026, 1, 2, 4, 4, 5, tzinfo=UTC),
            "description": None,
            "parent_task_id": None,
            "created_in_session_id": None,
            "claimed_by_session_id": None,
            "closed_in_session_id": None,
            "closed_commit_sha": None,
            "closed_at": "2026-01-03T03:04:05+00:00",
            "labels": "[]",
            "closed_reason": None,
            "validation_status": "pending",
            "validation_feedback": None,
            "category": "code",
            "validation_criteria": "verify datetime fields",
            "validation_fail_count": 0,
            "dispatch_failure_count": 0,
            "validation_override_reason": None,
            "merge_in_progress": False,
            "blocked_by_merge": False,
            "commits": None,
            "escalated_at": None,
            "escalation_reason": None,
            "is_escalated": False,
            "github_issue_number": None,
            "github_pr_number": None,
            "github_repo": None,
            "linear_issue_id": None,
            "linear_team_id": None,
            "seq_num": 17557,
            "path_cache": "17553.17557",
            "start_date": "2026-01-10",
            "due_date": "2026-01-20",
            "allow_automation": False,
            "unattended": False,
            "isolation": "worktree",
            "assigned_agent": None,
            "implementation_domain": "backend",
            "additional_skills": None,
        }
    )

    assert task.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert task.updated_at == datetime(2026, 1, 2, 4, 4, 5, tzinfo=UTC)
    assert task.closed_at == datetime(2026, 1, 3, 3, 4, 5, tzinfo=UTC)
    assert task.start_date == "2026-01-10"
    assert task.due_date == "2026-01-20"
    assert task.to_dict()["created_at"] == "2026-01-02T03:04:05+00:00"


def test_cron_job_from_row_normalizes_schedule_timestamps() -> None:
    job = CronJob.from_row(
        {
            "id": "33333333-3333-4333-8333-333333333333",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "name": "once",
            "description": None,
            "schedule_type": "once",
            "cron_expr": None,
            "interval_seconds": None,
            "run_at": "2026-01-02T03:04:05",
            "timezone": "UTC",
            "action_type": "handler",
            "action_config": "{}",
            "enabled": True,
            "is_system": False,
            "next_run_at": "2026-01-02T03:04:05+00:00",
            "last_run_at": None,
            "last_status": None,
            "consecutive_failures": 0,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:01",
        }
    )

    assert job.run_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert job.next_run_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert job.to_dict()["run_at"] == "2026-01-02T03:04:05+00:00"


def test_memory_from_row_normalizes_access_timestamps() -> None:
    memory = Memory.from_row(
        {
            "id": "44444444-4444-4444-8444-444444444444",
            "memory_type": "fact",
            "content": "storage timestamps are datetime-native",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T01:00:00+00:00",
            "project_id": None,
            "source_type": "agent",
            "source_session_id": None,
            "access_count": 1,
            "last_accessed_at": "2026-01-01T02:00:00",
            "tags": '["datetime"]',
            "deleted_at": None,
            "dream_action": None,
            "last_dreamed_at": None,
        }
    )

    assert memory.created_at == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert memory.last_accessed_at == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    assert memory.to_dict()["last_accessed_at"] == "2026-01-01T02:00:00+00:00"
