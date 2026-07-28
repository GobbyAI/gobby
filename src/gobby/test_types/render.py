"""Compact text rendering for test-type audit reports."""

from __future__ import annotations

from gobby.test_quality.baseline import AuditDiff
from gobby.test_quality.models import AuditIssue, AuditReport
from gobby.test_quality.render import render_json

_RANKED_FILE_LIMIT = 50
_NEW_ERROR_LIMIT = 100

__all__ = ["render_json", "render_text"]


def render_text(report: AuditReport, diff: AuditDiff | None = None) -> str:
    """Render bounded output suitable for local terminals and CI logs."""

    lines = [
        "Test types audit",
        f"Files scanned: {report.files_scanned}",
        f"Errors: {len(report.issues)}",
        _format_counts("Codes", report.issue_count_by_code),
    ]
    if diff is not None:
        lines.extend(
            [
                f"Baseline: {diff.baseline_status} ({diff.baseline_path})",
                f"Baseline mode: {diff.baseline_mode}",
                f"New errors: {len(diff.new_issues)}",
                f"Known baseline errors: {len(diff.known_issues)}",
                f"Failing new errors >= {diff.min_severity}: {len(diff.failing_issues)}",
            ]
        )
        if diff.warning_message:
            lines.append(diff.warning_message)

    if report.warnings:
        lines.extend(("", "Warnings:"))
        for warning in report.warnings:
            location = f"{warning.path}: " if warning.path else ""
            lines.append(f"  {warning.code} {location}{warning.message}")

    ranked = report.ranked_tests
    if ranked:
        lines.extend(("", "Ranked files:"))
        for item in ranked[:_RANKED_FILE_LIMIT]:
            codes = ", ".join(item.issue_codes)
            lines.append(f"  {item.score:>3} {item.path} [{codes}]")
        omitted = len(ranked) - _RANKED_FILE_LIMIT
        if omitted > 0:
            lines.append(f"  ... {omitted} additional files omitted")

    detailed_issues = report.sorted_issues if diff is None else diff.failing_issues
    if detailed_issues:
        heading = "Errors:" if diff is None else "New failing errors:"
        lines.extend(("", heading))
        _append_new_errors(lines, detailed_issues)
    return "\n".join(lines) + "\n"


def _append_new_errors(lines: list[str], issues: tuple[AuditIssue, ...]) -> None:
    for issue in issues[:_NEW_ERROR_LIMIT]:
        lines.append(f"  {issue.path}:{issue.line}: error: {issue.message} [{issue.issue_code}]")
    omitted = len(issues) - _NEW_ERROR_LIMIT
    if omitted > 0:
        lines.append(f"  ... {omitted} additional new errors omitted")


def _format_counts(label: str, counts: dict[str, int]) -> str:
    rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
    return f"{label}: {rendered or 'none'}"
