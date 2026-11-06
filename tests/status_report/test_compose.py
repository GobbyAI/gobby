"""Composition writes the report atomically and leaves nothing behind when the write fails."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.status_report.compose import REPORT_FILE, compose_report
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)


def test_failed_replace_removes_its_staging_file(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    reports = tmp_path / ".gobby" / "reports"
    (reports / REPORT_FILE).mkdir(parents=True)

    with pytest.raises(OSError):
        compose_report(
            temp_db,
            project_id=str(sample_project["id"]),
            project_path=tmp_path,
            integration_branch="0.5.0",
            operator_timezone="America/Chicago",
            window=timedelta(minutes=30),
            load_threshold=60.0,
            now=NOW,
        )

    assert list(reports.glob(f"{REPORT_FILE}*.tmp")) == []
