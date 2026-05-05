"""Baseline read/write and comparison helpers for test-quality audits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.test_quality.models import AuditIssue, AuditReport, Severity, severity_meets_minimum

BASELINE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class AuditBaseline:
    """Tracked known issues from a previous audit."""

    path: str
    fingerprints: frozenset[str]


@dataclass(frozen=True, slots=True)
class AuditDiff:
    """Issues that are present in a report and absent from the baseline."""

    new_issues: tuple[AuditIssue, ...]
    min_severity: Severity

    @property
    def failing_issues(self) -> tuple[AuditIssue, ...]:
        return tuple(
            issue
            for issue in self.new_issues
            if severity_meets_minimum(issue.severity, self.min_severity)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_severity": self.min_severity,
            "new_issue_count": len(self.new_issues),
            "failing_issue_count": len(self.failing_issues),
            "new_issues": [issue.to_dict() for issue in self.new_issues],
            "failing_issues": [issue.to_dict() for issue in self.failing_issues],
        }


def load_baseline(path: str | Path) -> AuditBaseline:
    baseline_path = Path(path)
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != BASELINE_SCHEMA_VERSION:
        msg = f"unsupported test-quality baseline schema: {data.get('schema_version')!r}"
        raise ValueError(msg)

    fingerprints = frozenset(
        item["fingerprint"]
        for item in data.get("issues", [])
        if isinstance(item, dict) and isinstance(item.get("fingerprint"), str)
    )
    return AuditBaseline(path=str(baseline_path), fingerprints=fingerprints)


def write_baseline(report: AuditReport, path: str | Path) -> None:
    baseline_path = Path(path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "issues": [issue.to_dict() for issue in report.sorted_issues],
    }
    baseline_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def diff_report(
    report: AuditReport,
    baseline: AuditBaseline,
    *,
    min_severity: Severity,
) -> AuditDiff:
    new_issues = tuple(
        issue for issue in report.sorted_issues if issue.fingerprint not in baseline.fingerprints
    )
    return AuditDiff(new_issues=new_issues, min_severity=min_severity)
