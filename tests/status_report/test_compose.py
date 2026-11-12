"""Composition writes the report atomically and leaves nothing behind when the write fails."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.status_report.compose import REPORT_FILE, compose_report
from gobby.status_report.delivery import ReportStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)


def test_failed_replace_removes_its_staging_file(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = tmp_path / ".gobby" / "reports"

    def fail_replace(source: str, target: Path) -> None:
        raise OSError("Synthetic publication failure")

    monkeypatch.setattr("gobby.status_report.compose.os.replace", fail_replace)

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


def test_distinct_artifacts_and_delta_from_delivered_full(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    def compose(at: datetime) -> dict[str, Any]:
        return compose_report(
            temp_db,
            project_id=str(sample_project["id"]),
            project_path=tmp_path,
            integration_branch="0.5.0",
            operator_timezone="America/Chicago",
            window=timedelta(minutes=30),
            load_threshold=10_000,
            now=at,
        )

    first = compose(NOW.replace(minute=0))
    unconfirmed = compose(NOW.replace(minute=30))
    assert first["file_path"] != unconfirmed["file_path"]
    assert unconfirmed["mode"] == "full"
    store = ReportStore(temp_db, str(sample_project["id"]), require_machine_id(), "operator")
    captured = store.capture_artifact(Path(first["file_path"]))
    assert captured is not None
    run_id, _, digest = captured
    assert store.acknowledge(run_id, str(uuid4()), digest)
    delta = compose(NOW.replace(minute=30))
    assert delta["mode"] == "delta"
    text = Path(delta["file_path"]).read_text()
    assert "No changes since the delivered full report." in text
    assert "Systems nominal" not in text
    assert Path(first["file_path"]).is_file()
