"""Output rendering for test-quality reports."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from gobby.test_quality.baseline import AuditDiff
from gobby.test_quality.models import AuditReport, AuditWarning


def render_json(report: AuditReport, diff: AuditDiff | None = None) -> str:
    report = _with_combined_warnings(report, diff)
    payload: dict[str, Any] = {"report": report.to_dict()}
    if diff is not None:
        payload["diff"] = diff.to_dict()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_text(report: AuditReport, diff: AuditDiff | None = None) -> str:
    lines = [
        "Test quality audit",
        f"Files scanned: {report.files_scanned}",
        f"Tests scanned: {report.tests_scanned}",
        f"Issues: {len(report.issues)}",
        _format_counts("Severity", report.issue_count_by_severity),
        _format_counts("Codes", report.issue_count_by_code),
    ]

    if diff is not None:
        lines.append(f"Baseline: {diff.baseline_status} ({diff.baseline_path})")
        lines.append(f"Baseline mode: {diff.baseline_mode}")
        if diff.warning_message:
            lines.append(diff.warning_message)
        lines.append(f"New issues: {len(diff.new_issues)}")
        lines.append(f"Failing new issues >= {diff.min_severity}: {len(diff.failing_issues)}")

    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in report.warnings:
            location = f"{warning.path}: " if warning.path else ""
            lines.append(f"  {warning.code} {location}{warning.message}")

    if report.ranked_tests:
        lines.append("")
        lines.append("Ranked tests:")
        for item in report.ranked_tests:
            codes = ", ".join(item.issue_codes)
            lines.append(f"  {item.score:>3} {item.identifier} [{codes}]")

    issues = (
        diff.failing_issues if diff is not None and diff.failing_issues else report.sorted_issues
    )
    if issues:
        heading = "Failing issues:" if diff is not None and diff.failing_issues else "Issues:"
        lines.append("")
        lines.append(heading)
        for issue in issues:
            lines.append(
                f"  {issue.severity.upper()} {issue.issue_code} "
                f"{issue.path}::{issue.test_name}:{issue.line} - {issue.message}"
            )

    return "\n".join(lines) + "\n"


def _format_counts(label: str, counts: dict[str, int]) -> str:
    if not counts:
        return f"{label}: none"
    rendered = ", ".join(f"{key}={value}" for key, value in counts.items() if value)
    return f"{label}: {rendered or 'none'}"


def _with_combined_warnings(report: AuditReport, diff: AuditDiff | None) -> AuditReport:
    warnings = _combined_warnings(report, diff)
    if warnings == report.warnings:
        return report
    return replace(report, warnings=warnings)


def _combined_warnings(report: AuditReport, diff: AuditDiff | None) -> tuple[AuditWarning, ...]:
    warnings = list(report.warnings)
    if diff is not None and diff.warning_message:
        warnings.append(
            AuditWarning(
                code="BASELINE_MISSING",
                path=_normalize_report_path(diff.baseline_path, report.root),
                message=diff.warning_message,
            )
        )
    return tuple(warnings)


def _normalize_report_path(path: str | None, root: str) -> str | None:
    if path is None:
        return None

    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()

    try:
        return candidate.resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return candidate.as_posix()
