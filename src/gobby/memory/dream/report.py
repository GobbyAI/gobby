"""Render one daily Dream synthesis from durable scope runs and decisions."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from gobby.memory.dream.decisions import DreamDecisionStore
from gobby.memory.dream.planner import normalize_summary
from gobby.memory.dream.storage_runs import _decode_run_row
from gobby.storage.hub.protocol import HubDatabase


def report_date(run: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(run["created_at"])).astimezone()


def report_path(root: str, run: dict[str, Any]) -> Path:
    directory = Path(root) / ".gobby/reports/dream"
    if run.get("dry_run"):
        directory /= "dry-run"
    return directory / f"gobby-dream-{report_date(run):%Y%m%d}.md"


def daily_runs(db: HubDatabase, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Select this scope's day once; resumed runs replace their own contribution."""
    day = report_date(run).date()
    start = datetime.combine(day, time.min).astimezone()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    rows = db.fetchall(
        "SELECT * FROM memory_dream_runs WHERE project_id IS NOT DISTINCT FROM %s "
        "AND dry_run = %s AND created_at >= %s AND created_at < %s "
        "ORDER BY created_at, id",
        (run.get("project_id"), bool(run.get("dry_run")), start, end),
    )
    return [
        decoded
        for row in rows
        if "scope_summaries" not in ((decoded := _decode_run_row(row)).get("summary") or {})
    ]


def previous_narrative(db: HubDatabase, run: dict[str, Any]) -> str:
    for previous in reversed(daily_runs(db, run)):
        if previous["id"] == run["id"]:
            continue
        narrative = normalize_summary((previous.get("summary") or {}).get("narrative"))
        if narrative:
            return narrative
    return ""


def render_report(db: HubDatabase, run: dict[str, Any]) -> str:
    runs = daily_runs(db, run)
    runs = [previous for previous in runs if previous["id"] != run["id"]] + [run]
    narrative = normalize_summary((run.get("summary") or {}).get("narrative"))
    narrative = narrative or previous_narrative(db, run) or "No narrative recorded."
    totals: Counter[str] = Counter()
    proposals: Counter[str] = Counter()
    decisions = DreamDecisionStore(db)
    evidence: list[str] = []
    failures: list[str] = []
    for contribution in runs:
        run_id = str(contribution["id"])
        summary = decisions.summary(run_id) or contribution.get("summary") or {}
        for key in ("mutations", "noops", "skipped", "errors"):
            totals[key] += int(summary.get(key, 0))
        proposals.update(summary.get("proposed_actions") or {})
        offset: int | None = 0
        while offset is not None:
            page = decisions.page(run_id, offset=offset, limit=100)
            for decision in page["decisions"]:
                action = decision.get("effective_action") or {}
                outcome = decision.get("outcome") or {}
                evidence.append(
                    f"- `{decision['id']}` — memory `{action.get('memory_id', '?')}`: "
                    f"{action.get('action', '?')} / {decision['status']}. "
                    f"{action.get('reason', 'No reason recorded.')} "
                    f"Recorded mutations: {outcome.get('mutations', 0)}."
                    + (f" Outcome: {outcome['reason']}" if outcome.get("reason") else "")
                    + (f" Error: {outcome['error']}" if outcome.get("error") else "")
                    + (
                        f" Snapshot: `{decision['snapshot_id']}`."
                        if decision.get("snapshot_id")
                        else ""
                    )
                )
            if page.get("historical_rationale_missing"):
                evidence.append(f"- Run `{run_id}`: detailed decision rationale is unavailable.")
                break
            offset = page["next_offset"]
        if contribution.get("error"):
            failures.append(f"- Run `{run_id}` ({contribution['status']}): {contribution['error']}")
    lines = [
        f"# Dream — {report_date(run):%Y-%m-%d}",
        "",
        f"Project: {run.get('project_id')}",
        "",
        "**Dry run: proposed decisions only; no mutations applied.**" if run.get("dry_run") else "",
        "## Synthesis",
        "",
        narrative,
        "",
        "## Recorded outcomes",
        "",
        ", ".join(f"{key}: {totals[key]}" for key in ("mutations", "noops", "skipped", "errors")),
        "",
        "Proposed actions: "
        + (", ".join(f"{key}: {count}" for key, count in sorted(proposals.items())) or "none"),
        "",
        "## Decisions and evidence",
        "",
        *(evidence or ["No decisions recorded."]),
        "",
        "## Run coverage and limitations",
        "",
        *(f"- `{item['id']}`: {item['status']}" for item in runs),
        *failures,
    ]
    return "\n".join(lines).strip() + "\n"
