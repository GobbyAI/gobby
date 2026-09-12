"""One cumulative feedback report per local review-start date."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from gobby.feedback.storage import FeedbackReviewRun, FeedbackRow

LEDGER_MARKER = "<!-- gobby-feedback-outcomes -->"


def daily_report_path(project_path: str, run: FeedbackReviewRun) -> str:
    directory = Path(project_path) / ".gobby/reports/feedback"
    if run.dry_run:
        directory /= "dry-run"
    date = run.created_at.astimezone().strftime("%Y%m%d")
    return str(directory / f"gobby-feedback-{date}.md")


def merge_report_inputs(
    runs: list[FeedbackReviewRun],
) -> tuple[list[FeedbackRow], dict[str, Any], dict[str, Any]]:
    """Overlay retried observations and retain unique recorded task outcomes."""
    rows: dict[str, FeedbackRow] = {}
    clusters: list[dict[str, Any]] = []
    outcomes: dict[str, dict[str, Any]] = {
        key: {} for key in ("filed", "suppressed", "skipped", "failed")
    }
    deduplicated = 0
    for run in runs:
        ids = {item["id"] for item in run.observations}
        clusters = [
            {**cluster, "observation_ids": remaining}
            for cluster in clusters
            if (remaining := [i for i in cluster["observation_ids"] if i not in ids])
        ]
        clusters.extend((run.findings or {}).get("clusters", []))
        for item in run.observations:
            data = dict(item)
            if isinstance(data["created_at"], str):
                data["created_at"] = datetime.fromisoformat(data["created_at"])
            rows[data["id"]] = FeedbackRow(**data)
        actions = run.actions or {}
        for key, entries in outcomes.items():
            for entry in actions.get(key, []):
                identity = (
                    str(entry.get("task_id") or entry) if isinstance(entry, dict) else str(entry)
                )
                entries[identity] = entry
        deduplicated += actions.get("deduplicated", 0)
    merged_actions: dict[str, Any] = {
        key: list(entries.values()) for key, entries in outcomes.items()
    }
    merged_actions["deduplicated"] = deduplicated
    return list(rows.values()), {"clusters": clusters}, merged_actions


def combine_report(summary_md: str, digest: str) -> str:
    """Replace the generated ledger when a reviewer carries it into its synthesis."""
    summary = summary_md.split(LEDGER_MARKER, 1)[0].rstrip()
    ledger = digest.replace(
        "# Session-feedback review digest", "## Review evidence and outcomes", 1
    )
    return f"{summary}\n\n{LEDGER_MARKER}\n\n{ledger}" if summary else digest
