"""Compose the operator status report and write it under the project's reports directory."""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from gobby.status_report.collect import active_runs, active_worktrees, collect_status
from gobby.status_report.delivery import ReportStore, delta_data, report_mode, semantic_snapshot
from gobby.status_report.render import is_nominal, render_markdown
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id

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
    destination_key: str = "operator",
) -> dict[str, Any]:
    """Collect, render and atomically write the report; return its path and nominal verdict."""
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.tasks.closure_snapshot import installed_project_policy

    policy_store = RuleDefinitionManager(db)
    policy = installed_project_policy(
        policy_store.list_effective(project_id),
        provenance=policy_store.list_all(project_id=project_id),
    )
    if policy is None:
        raise ValueError("Installed project policy is ambiguous or invalid")
    idle_age = policy.get(
        "idle_holder_age_seconds", policy.get("report_cadence_seconds", window.total_seconds())
    )
    if isinstance(idle_age, bool) or not isinstance(idle_age, (int, float)) or idle_age <= 0:
        raise ValueError("Idle-holder age must be a positive number of seconds")
    excluded_roles = policy.get("status_report_excluded_worktree_roles", ["main", "wiki"])
    if not isinstance(excluded_roles, list) or any(
        not isinstance(role, str) for role in excluded_roles
    ):
        raise ValueError("Report worktree role exclusions must be a list of strings")
    zone = ZoneInfo(operator_timezone)
    store = ReportStore(db, project_id, require_machine_id(), destination_key)
    baseline = store.baseline()
    mode = report_mode(policy, now)
    if mode == "delta" and baseline is None:
        mode = "full"
    run_id = store.reserve(mode, now)
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
        idle_holder_age_seconds=float(idle_age),
        excluded_worktree_roles=tuple(str(role) for role in excluded_roles),
    )
    snapshot = semantic_snapshot(data)
    nominal = is_nominal(data)
    if mode == "delta" and baseline is not None:
        data = delta_data(data, baseline.snapshot, baseline.created_at)
    text = render_markdown(data, zone)
    reports = project_path / ".gobby" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    target = reports / f"status-report-{run_id}-{uuid4().hex}.md"
    # A unique staging file keeps overlapping composes from interleaving or racing the replace.
    descriptor, staging = tempfile.mkstemp(dir=reports, prefix=REPORT_FILE, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(staging, target)
        try:
            store.publish(run_id, target, hashlib.sha256(text.encode()).hexdigest(), snapshot)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise
    return {"file_path": str(target), "nominal": nominal, "mode": mode, "run_id": run_id}
