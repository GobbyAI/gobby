"""Report models for the static test-quality audit."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Literal

Severity = Literal["low", "medium", "high"]

SEVERITY_WEIGHT: dict[Severity, int] = {"low": 1, "medium": 3, "high": 10}
SEVERITY_RANK: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True, slots=True)
class IssueDefinition:
    """Static metadata for an audit issue code."""

    code: str
    severity: Severity
    message: str


ISSUE_DEFINITIONS: dict[str, IssueDefinition] = {
    "NO_ASSERTION": IssueDefinition(
        "NO_ASSERTION",
        "high",
        "test has no assertion or assertion-like outcome check",
    ),
    "ASSERT_TRUE": IssueDefinition(
        "ASSERT_TRUE",
        "high",
        "test asserts a constant truthy value",
    ),
    "UNCONDITIONAL_SKIP": IssueDefinition(
        "UNCONDITIONAL_SKIP",
        "high",
        "test is skipped unconditionally",
    ),
    "XFAIL_WITHOUT_STRICT_OR_REASON": IssueDefinition(
        "XFAIL_WITHOUT_STRICT_OR_REASON",
        "high",
        "xfail requires both strict=True and a non-empty reason",
    ),
    "SLEEP_IN_TEST": IssueDefinition(
        "SLEEP_IN_TEST",
        "medium",
        "test uses sleep-based timing",
    ),
    "HEAVY_MOCK_LOW_ASSERT": IssueDefinition(
        "HEAVY_MOCK_LOW_ASSERT",
        "medium",
        "test has heavy mock setup with too little observable assertion coverage",
    ),
    "TODO_IN_TEST": IssueDefinition(
        "TODO_IN_TEST",
        "medium",
        "test contains TODO/FIXME/XXX debt marker",
    ),
    "ONLY_MOCK_ASSERTIONS": IssueDefinition(
        "ONLY_MOCK_ASSERTIONS",
        "medium",
        "test only verifies mock interactions",
    ),
}


@dataclass(frozen=True, slots=True)
class AuditIssue:
    """A single static quality issue attached to one test function."""

    path: str
    test_name: str
    issue_code: str
    severity: Severity
    line: int
    message: str

    @property
    def fingerprint(self) -> str:
        return f"{self.path}::{self.test_name}::{self.issue_code}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "path": self.path,
            "test_name": self.test_name,
            "issue_code": self.issue_code,
            "severity": self.severity,
            "line": self.line,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class RankedTest:
    """Aggregate issue score for one test function."""

    path: str
    test_name: str
    score: int
    issue_codes: tuple[str, ...]

    @property
    def identifier(self) -> str:
        return f"{self.path}::{self.test_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "path": self.path,
            "test_name": self.test_name,
            "score": self.score,
            "issue_codes": list(self.issue_codes),
        }


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Full audit result for a path set."""

    root: str
    paths: tuple[str, ...]
    issues: tuple[AuditIssue, ...]
    files_scanned: int
    tests_scanned: int

    @property
    def sorted_issues(self) -> tuple[AuditIssue, ...]:
        return tuple(sorted(self.issues, key=_issue_sort_key))

    @property
    def issue_count_by_code(self) -> dict[str, int]:
        return dict(sorted(Counter(issue.issue_code for issue in self.issues).items()))

    @property
    def issue_count_by_severity(self) -> dict[str, int]:
        counts = Counter(issue.severity for issue in self.issues)
        severities: tuple[Severity, ...] = ("high", "medium", "low")
        return {severity: counts.get(severity, 0) for severity in severities}

    @property
    def ranked_tests(self) -> tuple[RankedTest, ...]:
        scores: dict[tuple[str, str], int] = defaultdict(int)
        codes: dict[tuple[str, str], set[str]] = defaultdict(set)
        for issue in self.issues:
            key = (issue.path, issue.test_name)
            scores[key] += SEVERITY_WEIGHT[issue.severity]
            codes[key].add(issue.issue_code)

        ranked = [
            RankedTest(
                path=path,
                test_name=test_name,
                score=score,
                issue_codes=tuple(sorted(codes[(path, test_name)])),
            )
            for (path, test_name), score in scores.items()
        ]
        return tuple(sorted(ranked, key=lambda item: (-item.score, item.path, item.test_name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "paths": list(self.paths),
            "files_scanned": self.files_scanned,
            "tests_scanned": self.tests_scanned,
            "issue_count": len(self.issues),
            "issue_count_by_severity": self.issue_count_by_severity,
            "issue_count_by_code": self.issue_count_by_code,
            "ranked_tests": [item.to_dict() for item in self.ranked_tests],
            "issues": [issue.to_dict() for issue in self.sorted_issues],
        }


def severity_meets_minimum(severity: Severity, minimum: Severity) -> bool:
    return SEVERITY_RANK[severity] >= SEVERITY_RANK[minimum]


def _issue_sort_key(issue: AuditIssue) -> tuple[int, str, str, str, int]:
    return (
        -SEVERITY_RANK[issue.severity],
        issue.path,
        issue.test_name,
        issue.issue_code,
        issue.line,
    )
