from __future__ import annotations

from copy import deepcopy
from pathlib import Path
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
from gobby.plans.review_requirements import assemble_requirements_bundle


def _evidence(
    prior_round_context: dict[str, object] | None = None,
) -> PlanReviewEvidence:
    return cast(
        PlanReviewEvidence,
        SimpleNamespace(
            evidence_id="evidence-1",
            plan_hash="a" * 64,
            section_manifest=(SectionHash(section_id="1.1", section_hash="b" * 64),),
            prior_round_context=prior_round_context,
        ),
    )


def _finding(
    *,
    severity: str = "major",
    repair_scope: str = "existing_sections",
) -> dict[str, object]:
    return {
        "finding_id": "F1",
        "section_id": "1.1",
        "check_key": "failure-atomicity",
        "severity": severity,
        "category": "unhandled-edge",
        "location": "§ 1.1",
        "description": "The failure path can leave partial state.",
        "minimal_repair": "Specify rollback before retry.",
        "repair_scope": repair_scope,
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


def test_failure_trace_accepts_bound_requirement_citation(tmp_path: Path) -> None:
    bundle = assemble_requirements_bundle(
        project_root=tmp_path,
        plan_snapshot=b"# Plan\n",
        task_id="task-1",
        task_fields={
            "title": "Immutable requirement",
            "description": "Description",
            "validation_criteria": "Acceptance",
        },
    )
    sources = bundle["sources"]
    assert isinstance(sources, list)
    source = sources[0]
    assert isinstance(source, dict)
    finding = _finding(severity="blocking")
    trace = _failure_trace()
    trace["citation"] = [
        {
            "requirement_id": source["requirement_id"],
            "content_sha256": source["content_sha256"],
        }
    ]
    finding["failure_trace"] = trace

    validated = validate_plan_review_findings(
        [finding],
        evidence=_evidence({"requirements_bundle": bundle}),
    )

    assert validated[0]["failure_trace"] == trace

    tampered = deepcopy(finding)
    tampered_trace = cast(dict[str, object], tampered["failure_trace"])
    tampered_citations = cast(list[dict[str, object]], tampered_trace["citation"])
    tampered_citations[0]["content_sha256"] = "f" * 64
    with pytest.raises(ReviewEvidenceError):
        validate_plan_review_findings(
            [tampered],
            evidence=_evidence({"requirements_bundle": bundle}),
        )


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


def test_minimal_repair_required() -> None:
    for required_field in ("minimal_repair", "repair_scope"):
        incomplete = _finding()
        incomplete.pop(required_field)
        with pytest.raises(ReviewEvidenceError, match=required_field):
            validate_plan_review_findings([incomplete], evidence=_evidence())

    invalid_scope = _finding(repair_scope="whole_plan")
    with pytest.raises(ReviewEvidenceError, match="repair_scope"):
        validate_plan_review_findings([invalid_scope], evidence=_evidence())

    existing = _finding()
    assert validate_plan_review_findings([existing], evidence=_evidence())[0] == existing

    existing["new_deliverable_justification"] = "A separate artifact is easier to find."
    with pytest.raises(ReviewEvidenceError, match="new_deliverable_justification"):
        validate_plan_review_findings([existing], evidence=_evidence())

    new_deliverable = _finding(repair_scope="new_deliverable")
    with pytest.raises(ReviewEvidenceError, match="new_deliverable_justification"):
        validate_plan_review_findings([new_deliverable], evidence=_evidence())

    new_deliverable["new_deliverable_justification"] = (
        "No existing section owns the new operator runbook."
    )
    assert (
        validate_plan_review_findings([new_deliverable], evidence=_evidence())[0] == new_deliverable
    )
