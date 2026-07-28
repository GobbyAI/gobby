from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    SectionHash,
)
from gobby.plans.review_findings import (
    FINDING_SEVERITIES,
    render_rejection_section,
    validate_plan_review_findings,
)


def _evidence() -> PlanReviewEvidence:
    return cast(
        PlanReviewEvidence,
        SimpleNamespace(
            evidence_id="evidence-1",
            plan_hash="a" * 64,
            section_manifest=(SectionHash(section_id="1.1", section_hash="b" * 64),),
        ),
    )


def _finding(*, severity: str = "major") -> dict[str, object]:
    return {
        "finding_id": "F1",
        "section_id": "1.1",
        "check_key": "failure-atomicity",
        "severity": severity,
        "category": "unhandled-edge",
        "location": "§ 1.1",
        "description": "The failure path can leave partial state.",
        "minimal_repair": "Specify rollback before retry.",
        "principle": "Failure handling must be atomic.",
        "prevention": "Walk every write failure boundary.",
    }


def _failure_trace() -> dict[str, object]:
    return {
        "preconditions": "The first durable write succeeds.",
        "action": "The second durable write fails.",
        "wrong_outcome": "The first write remains visible.",
        "violated_obligation": "The operation must commit atomically.",
        "citation": [
            {
                "path": "src/gobby/plans/review_findings.py",
                "sha256": "c" * 64,
                "line_start": 117,
                "line_end": 158,
            }
        ],
    }


def test_finding_severities_are_four_tier_vocabulary() -> None:
    assert FINDING_SEVERITIES == frozenset({"blocking", "major", "minor", "nit"})


def test_blocking_requires_failure_trace() -> None:
    with pytest.raises(ReviewEvidenceError, match=r"failure_trace.*preconditions"):
        validate_plan_review_findings([_finding(severity="blocking")], evidence=_evidence())


@pytest.mark.parametrize("severity", sorted(FINDING_SEVERITIES))
def test_failure_trace_all_or_nothing(severity: str) -> None:
    if severity in {"major", "minor"}:
        assert (
            validate_plan_review_findings(
                [_finding(severity=severity)],
                evidence=_evidence(),
            )[0]["severity"]
            == severity
        )

    finding = _finding(severity=severity)
    trace = _failure_trace()
    del trace["action"]
    finding["failure_trace"] = trace

    with pytest.raises(ReviewEvidenceError, match=r"failure_trace\.action"):
        validate_plan_review_findings([finding], evidence=_evidence())

    finding = _finding(severity=severity)
    trace = _failure_trace()
    citations = cast(list[dict[str, object]], trace["citation"])
    citations[0]["sha256"] = "C" * 64
    finding["failure_trace"] = trace

    with pytest.raises(ReviewEvidenceError, match=r"failure_trace\.citation\[0\]\.sha256"):
        validate_plan_review_findings([finding], evidence=_evidence())


def test_invalid_severity_diagnostic_derives_from_constant() -> None:
    finding = _finding(severity="cosmetic")
    vocabulary = ", ".join(sorted(FINDING_SEVERITIES))

    with pytest.raises(ReviewEvidenceError, match=vocabulary):
        validate_plan_review_findings([finding], evidence=_evidence())


def test_single_canonical_remedy_field() -> None:
    finding = _finding()
    validated = validate_plan_review_findings([finding], evidence=_evidence())
    rendered = render_rejection_section(
        round_number=1,
        findings=validated,
        evidence=_evidence(),
    )

    assert validated[0]["minimal_repair"] == "Specify rollback before retry."
    assert "**Minimal repair:** Specify rollback before retry." in rendered
    for legacy_field in ("fix", "suggested_fix"):
        legacy = _finding()
        legacy[legacy_field] = legacy.pop("minimal_repair")
        with pytest.raises(ReviewEvidenceError, match=legacy_field):
            validate_plan_review_findings([legacy], evidence=_evidence())
