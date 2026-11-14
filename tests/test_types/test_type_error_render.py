from __future__ import annotations

import pytest

from gobby.test_quality.baseline import AuditBaseline, diff_report
from gobby.test_quality.models import AuditIssue, AuditReport
from gobby.test_types.render import render_text

pytestmark = pytest.mark.unit


def _report(count: int) -> AuditReport:
    issues = tuple(
        AuditIssue(
            path=f"tests/test_{index:03}.py",
            test_name="",
            issue_code="mypy:arg-type",
            severity="high",
            line=index + 1,
            message=f"failure {index}",
        )
        for index in range(count)
    )
    return AuditReport(
        root="/project",
        paths=("tests",),
        issues=issues,
        files_scanned=count,
        tests_scanned=0,
    )


def test_render_text_caps_ranked_files_and_omits_known_issue_detail() -> None:
    report = _report(55)
    baseline = AuditBaseline(
        path="baseline.json",
        issue_counts={issue.fingerprint: 1 for issue in report.issues},
    )

    rendered = render_text(report, diff_report(report, baseline, min_severity="high"))

    assert "Test types audit" in rendered
    assert "Ranked files:" in rendered
    assert "... 5 additional files omitted" in rendered
    assert "Known baseline errors: 55" in rendered
    assert "failure 0" not in rendered
    assert "New failing errors:" not in rendered


def test_render_text_caps_detailed_new_errors() -> None:
    report = _report(105)
    baseline = AuditBaseline(path="baseline.json", issue_counts={})

    rendered = render_text(report, diff_report(report, baseline, min_severity="high"))

    assert "New failing errors:" in rendered
    assert rendered.count(": error: ") == 100
    assert "... 5 additional new errors omitted" in rendered


def test_render_text_without_baseline_labels_all_errors() -> None:
    assert "\nErrors:\n" in render_text(_report(1))
