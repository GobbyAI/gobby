"""Tests for cron job and run dataclasses and croniter dependency."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.storage.cron_models import CronJob, CronRun

pytestmark = pytest.mark.unit


# --- croniter dependency tests (#7618) ---


def test_croniter_import_and_basic_parsing() -> None:
    """Verify croniter is installed and can parse a basic cron expression."""
    from croniter import croniter

    base = datetime(2026, 2, 10, 12, 0, 0, tzinfo=UTC)
    cron = croniter("0 7 * * *", base)
    next_run = cron.get_next(datetime)
    assert next_run.hour == 7
    assert next_run.day == 11  # next day since base is 12:00


def test_croniter_every_5_minutes() -> None:
    """Verify croniter handles minute-level expressions."""
    from croniter import croniter

    base = datetime(2026, 2, 10, 12, 0, 0, tzinfo=UTC)
    cron = croniter("*/5 * * * *", base)
    next_run = cron.get_next(datetime)
    assert next_run.minute == 5
    assert next_run.hour == 12


# --- CronJob dataclass tests (#7619) ---


def _make_mock_row(data: dict) -> MagicMock:
    """Create a mock sqlite3.Row with given data."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.keys = lambda: list(data.keys())
    return row


def test_cron_job_creation() -> None:
    """CronJob can be created with all required fields."""
    job = CronJob(
        id="cj-abc123",
        project_id="proj-1",
        name="Morning Email",
        schedule_type="cron",
        action_type="agent_spawn",
        action_config={"prompt": "check email", "provider": "claude"},
        created_at="2026-02-10T12:00:00+00:00",
        updated_at="2026-02-10T12:00:00+00:00",
        cron_expr="0 7 * * *",
        timezone="America/Los_Angeles",
    )
    assert job.id == "cj-abc123"
    assert job.name == "Morning Email"
    assert job.schedule_type == "cron"
    assert job.action_type == "agent_spawn"
    assert job.action_config["prompt"] == "check email"
    assert job.enabled is True
    assert job.consecutive_failures == 0


def test_cron_job_from_row() -> None:
    """CronJob.from_row() deserializes from a database row."""
    row_data = {
        "id": "cj-abc123",
        "project_id": "proj-1",
        "name": "Test Job",
        "description": "A test cron job",
        "schedule_type": "interval",
        "cron_expr": None,
        "interval_seconds": 300,
        "run_at": None,
        "timezone": "UTC",
        "action_type": "shell",
        "action_config": json.dumps({"command": "echo", "args": ["hello"]}),
        "enabled": 1,
        "next_run_at": "2026-02-10T12:05:00+00:00",
        "last_run_at": "2026-02-10T12:00:00+00:00",
        "last_status": "completed",
        "consecutive_failures": 0,
        "created_at": "2026-02-10T12:00:00+00:00",
        "updated_at": "2026-02-10T12:00:00+00:00",
    }
    row = _make_mock_row(row_data)
    job = CronJob.from_row(row)
    assert job.id == "cj-abc123"
    assert job.interval_seconds == 300
    assert job.action_config == {"command": "echo", "args": ["hello"]}
    assert job.enabled is True
    assert job.last_status == "completed"


def test_cron_job_to_dict() -> None:
    """CronJob.to_dict() serializes all fields."""
    job = CronJob(
        id="cj-abc123",
        project_id="proj-1",
        name="Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        created_at="2026-02-10T12:00:00",
        updated_at="2026-02-10T12:00:00",
        cron_expr="0 7 * * *",
    )
    d = job.to_dict()
    assert d["id"] == "cj-abc123"
    assert d["name"] == "Test"
    assert d["schedule_type"] == "cron"
    assert d["cron_expr"] == "0 7 * * *"
    assert d["action_config"] == {"command": "echo"}
    assert "project_id" in d
    assert "created_at" in d


def test_cron_job_to_brief() -> None:
    """CronJob.to_brief() returns minimal fields."""
    job = CronJob(
        id="cj-abc123",
        project_id="proj-1",
        name="Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        created_at="2026-02-10T12:00:00",
        updated_at="2026-02-10T12:00:00",
        cron_expr="0 7 * * *",
    )
    brief = job.to_brief()
    assert brief["id"] == "cj-abc123"
    assert brief["name"] == "Test"
    assert "project_id" not in brief
    assert "description" not in brief
    assert "created_at" not in brief


def test_cron_job_action_config_json_roundtrip() -> None:
    """action_config survives JSON serialization round-trip."""
    config = {
        "prompt": "Do analysis",
        "provider": "claude",
        "model": "sonnet",
        "timeout_seconds": 300,
        "nested": {"key": "value"},
    }
    row_data = {
        "id": "cj-1",
        "project_id": "p",
        "name": "test",
        "description": None,
        "schedule_type": "cron",
        "cron_expr": "0 * * * *",
        "interval_seconds": None,
        "run_at": None,
        "timezone": "UTC",
        "action_type": "agent_spawn",
        "action_config": json.dumps(config),
        "enabled": 1,
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
        "consecutive_failures": 0,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    row = _make_mock_row(row_data)
    job = CronJob.from_row(row)
    assert job.action_config == config
    assert job.action_config["nested"]["key"] == "value"


# --- CronRun dataclass tests (#7619) ---


def test_cron_run_creation() -> None:
    """CronRun can be created with required fields."""
    run = CronRun(
        id="cr-abc123",
        cron_job_id="cj-abc123",
        triggered_at="2026-02-10T12:00:00+00:00",
        created_at="2026-02-10T12:00:00+00:00",
    )
    assert run.id == "cr-abc123"
    assert run.status == "pending"
    assert run.output is None
    assert run.error is None


def test_cron_run_from_row() -> None:
    """CronRun.from_row() deserializes from a database row."""
    row_data = {
        "id": "cr-abc123",
        "cron_job_id": "cj-abc123",
        "triggered_at": "2026-02-10T12:00:00+00:00",
        "started_at": "2026-02-10T12:00:01+00:00",
        "completed_at": "2026-02-10T12:02:30+00:00",
        "status": "completed",
        "output": "Job completed successfully",
        "error": None,
        "agent_run_id": "ar-123",
        "pipeline_execution_id": None,
        "created_at": "2026-02-10T12:00:00+00:00",
    }
    row = _make_mock_row(row_data)
    run = CronRun.from_row(row)
    assert run.id == "cr-abc123"
    assert run.status == "completed"
    assert run.output == "Job completed successfully"
    assert run.agent_run_id == "ar-123"


def test_cron_run_to_dict() -> None:
    """CronRun.to_dict() serializes all fields."""
    run = CronRun(
        id="cr-abc123",
        cron_job_id="cj-abc123",
        triggered_at="2026-02-10T12:00:00+00:00",
        created_at="2026-02-10T12:00:00+00:00",
        status="failed",
        error="Timeout",
    )
    d = run.to_dict()
    assert d["id"] == "cr-abc123"
    assert d["status"] == "failed"
    assert d["error"] == "Timeout"
    assert "cron_job_id" in d
    assert "triggered_at" in d
