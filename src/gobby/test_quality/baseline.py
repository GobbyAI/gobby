"""Baseline read/write and comparison helpers for test-quality audits."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.test_quality.models import AuditIssue, AuditReport, Severity, severity_meets_minimum

BASELINE_SCHEMA_VERSION = 2
BASELINE_MISSING_MESSAGE = "Baseline missing; treating current issues as new"


@dataclass(frozen=True, slots=True)
class AuditBaseline:
    """Tracked known issues from a previous audit."""

    path: str
    issue_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class AuditDiff:
    """Issues that are present in a report and absent from the baseline."""

    new_issues: tuple[AuditIssue, ...]
    known_issues: tuple[AuditIssue, ...]
    min_severity: Severity
    baseline_path: str | None = None
    baseline_status: str = "loaded"
    baseline_mode: str = "diff"
    warning_message: str | None = None

    @property
    def failing_issues(self) -> tuple[AuditIssue, ...]:
        return tuple(
            issue
            for issue in self.new_issues
            if severity_meets_minimum(issue.severity, self.min_severity)
        )

    @property
    def below_threshold_issues(self) -> tuple[AuditIssue, ...]:
        return tuple(
            issue
            for issue in self.new_issues
            if not severity_meets_minimum(issue.severity, self.min_severity)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": {
                "mode": self.baseline_mode,
                "path": self.baseline_path,
                "status": self.baseline_status,
                "message": self.warning_message,
            },
            "min_severity": self.min_severity,
            "new_issue_count": len(self.new_issues),
            "known_issue_count": len(self.known_issues),
            "failing_issue_count": len(self.failing_issues),
            "new_issues": [issue.to_dict() for issue in self.new_issues],
            "known_issues": [issue.to_dict() for issue in self.known_issues],
            "failing_issues": [issue.to_dict() for issue in self.failing_issues],
        }


def load_baseline(path: str | Path) -> AuditBaseline:
    baseline_path = Path(path)
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != BASELINE_SCHEMA_VERSION:
        msg = f"unsupported test-quality baseline schema: {data.get('schema_version')!r}"
        raise ValueError(msg)

    issue_counts: Counter[str] = Counter()
    for item in data.get("issues", []):
        if not isinstance(item, dict) or not isinstance(item.get("fingerprint"), str):
            continue
        occurrences = item.get("occurrences")
        if type(occurrences) is not int or occurrences < 1:
            msg = "test-quality baseline issue occurrences must be positive integers"
            raise ValueError(msg)
        issue_counts[item["fingerprint"]] += occurrences
    return AuditBaseline(path=str(baseline_path), issue_counts=dict(issue_counts))


def write_baseline(report: AuditReport, path: str | Path) -> None:
    baseline_path = Path(path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict[str, Any]] = {}
    for issue in report.sorted_issues:
        if issue.fingerprint in entries:
            entries[issue.fingerprint]["occurrences"] += 1
            continue
        entries[issue.fingerprint] = {**issue.to_dict(), "occurrences": 1}
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "issues": list(entries.values()),
    }
    baseline_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def diff_report(
    report: AuditReport,
    baseline: AuditBaseline,
    *,
    min_severity: Severity,
    baseline_status: str = "loaded",
    baseline_mode: str = "diff",
    warning_message: str | None = None,
) -> AuditDiff:
    remaining_counts = Counter(baseline.issue_counts)
    new_issues: list[AuditIssue] = []
    known_issues: list[AuditIssue] = []
    for issue in report.sorted_issues:
        if remaining_counts[issue.fingerprint] > 0:
            known_issues.append(issue)
            remaining_counts[issue.fingerprint] -= 1
        else:
            new_issues.append(issue)
    return AuditDiff(
        new_issues=tuple(new_issues),
        known_issues=tuple(known_issues),
        min_severity=min_severity,
        baseline_path=baseline.path,
        baseline_status=baseline_status,
        baseline_mode=baseline_mode,
        warning_message=warning_message,
    )
