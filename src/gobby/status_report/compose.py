"""Compose the operator status report and write it under the project's reports directory."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gobby.status_report.collect import active_runs, active_worktrees, collect_status
from gobby.status_report.render import is_nominal, render_markdown
from gobby.storage.hub.protocol import HubDatabase

REPORT_FILE = "status-report.md"


def compose_report(
    db: HubDatabase,
    *,
    project_id: str,
    project_path: Path,
    integration_branch: str,
    operator_timezone: str,
    window: timedelta,
    load_threshold: float,
    now: datetime,
) -> dict[str, Any]:
    """Collect, render and atomically write the report; return its path and nominal verdict."""
    zone = ZoneInfo(operator_timezone)
    data = collect_status(
        db,
        project_id=project_id,
        project_path=project_path,
        integration_branch=integration_branch,
        now=now,
        window=window,
        load_threshold=load_threshold,
        runs=active_runs(db),
        worktrees=active_worktrees(db, project_id),
        loadavg=os.getloadavg,
    )
    text = render_markdown(data, zone)
    reports = project_path / ".gobby" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    target = reports / REPORT_FILE
    # A unique staging file keeps overlapping composes from interleaving or racing the replace.
    descriptor, staging = tempfile.mkstemp(dir=reports, prefix=REPORT_FILE, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(staging, target)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise
    return {"file_path": str(target), "nominal": is_nominal(data)}
