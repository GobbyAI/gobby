"""Tests for CronRun.to_brief() slim representation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.cron_models import CronRun, CronRunChild

pytestmark = pytest.mark.unit

_TRIGGERED_AT = datetime(2026, 1, 22, 0, 0, 0, tzinfo=UTC)
_STARTED_AT = datetime(2026, 1, 22, 0, 0, 1, tzinfo=UTC)
_COMPLETED_AT = datetime(2026, 1, 22, 0, 1, 0, tzinfo=UTC)


class TestCronRunToBrief:
    """Tests for CronRun.to_brief() slim representation."""

    def test_to_brief_has_fewer_fields_than_to_dict(self) -> None:
        """to_brief returns fewer fields than to_dict."""
        run = CronRun(
            id="run-123",
            cron_job_id="job-456",
            triggered_at=_TRIGGERED_AT,
            created_at=_TRIGGERED_AT,
            started_at=_STARTED_AT,
            completed_at=_COMPLETED_AT,
            status="completed",
            output="Long output string that could be very large...",
            error=None,
            agent_run_id="ar-789",
            pipeline_execution_id="pe-abc",
        )

        brief = run.to_brief()
        full = run.to_dict()
        assert len(brief) < len(full)

    def test_to_brief_essential_fields_present(self) -> None:
        """to_brief includes essential status and identification fields."""
        run = CronRun(
            id="run-brief",
            cron_job_id="job-brief",
            triggered_at=_TRIGGERED_AT,
            created_at=_TRIGGERED_AT,
            started_at=_STARTED_AT,
            completed_at=_COMPLETED_AT,
            status="failed",
            error="Connection timeout",
            agent_run_id="ar-abc",
            pipeline_execution_id="pe-xyz",
        )

        brief = run.to_brief()
        assert brief["id"] == "run-brief"
        assert brief["cron_job_id"] == "job-brief"
        assert brief["status"] == "failed"
        assert brief["started_at"] == "2026-01-22T00:00:01+00:00"
        assert brief["completed_at"] == "2026-01-22T00:01:00+00:00"
        assert brief["error"] == "Connection timeout"
        assert brief["agent_run_id"] == "ar-abc"
        assert brief["pipeline_execution_id"] == "pe-xyz"
        assert brief["child"] is None

    def test_to_brief_excludes_output_and_timestamps(self) -> None:
        """to_brief omits output (can be large), triggered_at, and created_at."""
        run = CronRun(
            id="run-exc",
            cron_job_id="job-exc",
            triggered_at=_TRIGGERED_AT,
            created_at=_TRIGGERED_AT,
            output="Very large output that would waste tokens...",
        )

        brief = run.to_brief()
        assert "output" not in brief
        assert "triggered_at" not in brief
        assert "created_at" not in brief

    def test_to_brief_includes_child_projection(self) -> None:
        """to_brief includes hydrated child state when present."""
        run = CronRun(
            id="run-child",
            cron_job_id="job-child",
            triggered_at=_TRIGGERED_AT,
            created_at=_TRIGGERED_AT,
            status="dispatched",
            pipeline_execution_id="pe-child",
            child=CronRunChild(
                type="pipeline_execution",
                id="pe-child",
                status="waiting_approval",
                terminal=False,
                missing=False,
            ),
        )

        brief = run.to_brief()

        assert brief["status"] == "dispatched"
        assert brief["child"] == {
            "type": "pipeline_execution",
            "id": "pe-child",
            "status": "waiting_approval",
            "terminal": False,
            "missing": False,
        }
