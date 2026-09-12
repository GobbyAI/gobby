"""Shared daily report naming and retry-safe evidence merging."""

from dataclasses import asdict, replace
from datetime import UTC, datetime

from gobby.feedback.report import combine_report, daily_report_path, merge_report_inputs
from gobby.feedback.storage import FeedbackReviewRun, FeedbackRow


def _run(run_id: str, observation: str, task: str) -> FeedbackReviewRun:
    now = datetime(2026, 9, 11, 23, 59).astimezone().astimezone(UTC)
    row = FeedbackRow(
        id=observation,
        session_id="session",
        source="cli:test",
        kind="friction",
        kind_other_label=None,
        evidence="Observed failure",
        impact="Blocked work",
        frequency="once",
        suggestion=None,
        disposition=None,
        created_at=now,
    )
    return FeedbackReviewRun(
        id=run_id,
        status="completed",
        dry_run=False,
        window_start=now,
        window_end=now,
        rows_considered=1,
        findings={"clusters": [{"observation_ids": [observation], "theme": run_id}]},
        actions={"filed": [{"task_id": task, "task_ref": f"#{task}"}]},
        digest_md="Summary",
        error=None,
        created_at=now,
        completed_at=now,
        observations=[asdict(row)],
    )


def test_daily_path_uses_local_start_date_and_isolates_dry_run() -> None:
    run = _run("first", "observation", "1")
    next_day = datetime(2026, 9, 12, 0, 1).astimezone().astimezone(UTC)
    assert daily_report_path("/project", replace(run, completed_at=next_day)) == (
        "/project/.gobby/reports/feedback/gobby-feedback-20260911.md"
    )
    assert daily_report_path("/project", replace(run, created_at=next_day)) == (
        "/project/.gobby/reports/feedback/gobby-feedback-20260912.md"
    )
    assert daily_report_path("/project", replace(run, dry_run=True)) == (
        "/project/.gobby/reports/feedback/dry-run/gobby-feedback-20260911.md"
    )


def test_merge_preserves_prior_tasks_and_overlays_retried_observations() -> None:
    first = _run("first", "a", "1")
    second = _run("second", "b", "2")
    retry = replace(
        first,
        id="retry",
        findings={"clusters": [{"observation_ids": ["a"], "theme": "Verified again"}]},
    )
    rows, findings, actions = merge_report_inputs([first, second, retry])
    assert [row.id for row in rows] == ["a", "b"]
    assert findings == {
        "clusters": [
            {"observation_ids": ["b"], "theme": "second"},
            {"observation_ids": ["a"], "theme": "Verified again"},
        ]
    }
    assert actions["filed"] == [
        {"task_id": "1", "task_ref": "#1"},
        {"task_id": "2", "task_ref": "#2"},
    ]


def test_generated_outcomes_are_replaced_instead_of_nested() -> None:
    previous = combine_report("# Synthesis\nBoth concerns remain.", "old outcomes")
    revised = combine_report(previous, "new outcomes")
    assert revised == (
        "# Synthesis\nBoth concerns remain.\n\n<!-- gobby-feedback-outcomes -->\n\nnew outcomes"
    )
