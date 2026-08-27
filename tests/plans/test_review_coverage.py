from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from gobby.plans.manifest_emitter import derive_manifest_entries
from gobby.plans.parser import Kind, PlanDocument, parse_plan
from gobby.plans.review_coverage import (
    REVIEW_LANES,
    review_complexity,
    validate_coverage_attestation,
    validate_review_coverage,
)
from gobby.plans.review_evidence_models import ReviewEvidenceError
from tests.review_coverage_helpers import coverage_attestation, manifest_digest


def _document(
    tmp_path: Path,
    *,
    deliverable_count: int = 1,
    acceptance_count: int | None = None,
    target_count: int = 1,
) -> PlanDocument:
    total_acceptance = acceptance_count or deliverable_count
    assert total_acceptance >= deliverable_count
    lines = [
        "# Coverage Plan",
        "**Plan ID:** coverage-plan",
        "",
        "## P1 Phase",
        "`kind: framing`",
        "",
    ]
    remaining = total_acceptance
    acceptance_index = 0
    for deliverable_index in range(1, deliverable_count + 1):
        remaining_deliverables = deliverable_count - deliverable_index
        item_count = remaining - remaining_deliverables if deliverable_index == 1 else 1
        remaining -= item_count
        section_id = f"1.{deliverable_index}"
        lines.extend(
            [
                f"### {section_id} Deliverable {deliverable_index}",
                "`kind: deliverable`",
                "",
                *(
                    [
                        "Targets:",
                        *[
                            f"- `src/target_{target_index}.py`"
                            for target_index in range(1, target_count + 1)
                        ],
                        "",
                    ]
                    if deliverable_index == 1
                    else []
                ),
                "**Acceptance:**",
            ]
        )
        for item_index in range(1, item_count + 1):
            acceptance_index += 1
            target_index = (acceptance_index - 1) % target_count + 1
            lines.append(
                f"- {section_id}.{item_index} — Requirement {acceptance_index}. "
                f"file: `src/target_{target_index}.py`"
            )
        lines.append("")
    path = tmp_path / (f"plan-{deliverable_count}-{total_acceptance}-{target_count}.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return parse_plan(path, parse_mode="draft")


@pytest.mark.parametrize(
    ("document_kwargs", "changed_sections", "expected_mode"),
    [
        ({"deliverable_count": 7}, 0, "sequential"),
        ({"deliverable_count": 8}, 0, "parallel"),
        ({"acceptance_count": 23, "target_count": 1}, 0, "sequential"),
        ({"acceptance_count": 24, "target_count": 1}, 0, "parallel"),
        ({"acceptance_count": 11, "target_count": 11}, 0, "sequential"),
        ({"acceptance_count": 12, "target_count": 12}, 0, "parallel"),
        ({}, 3, "sequential"),
        ({}, 4, "parallel"),
    ],
)
def test_review_complexity_threshold_boundaries(
    tmp_path: Path,
    document_kwargs: dict[str, int],
    changed_sections: int,
    expected_mode: str,
) -> None:
    result = review_complexity(
        _document(tmp_path, **document_kwargs),
        changed_section_count=changed_sections,
    )

    assert result["mode"] == expected_mode
    assert result["lanes"] == list(REVIEW_LANES)
    assert result["max_workers"] == (3 if expected_mode == "parallel" else 0)


def _coverage_case(
    tmp_path: Path,
    *,
    candidate_count: int = 1,
) -> tuple[
    PlanDocument,
    list[object],
    dict[str, object],
    dict[str, object],
]:
    document = _document(tmp_path, deliverable_count=2)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    citation: dict[str, object] = {
        "path": "src/example.py",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "line_start": 1,
        "line_end": 1,
    }
    section_ids = [
        section.section_id for section in document.sections if section.kind is Kind.deliverable
    ]
    candidates = [
        {
            "candidate_id": f"candidate-{index}",
            "section_ids": [section_ids[0]],
            "violated_invariant": f"Invariant {index}",
            "source_citations": [citation],
            "suggested_fix": f"Fix {index}",
            "adjacent_sites_checked": ["src/adjacent.py"],
            "confidence": 0.9,
        }
        for index in range(1, candidate_count + 1)
    ]
    lanes: list[object] = [
        {
            "lane_id": lane_id,
            "status": (
                "delegated-verified" if lane_id == "repository_blast_radius" else "completed"
            ),
            "section_ids_checked": section_ids,
            "source_citations": [citation],
            "candidate_issues": candidates if lane_id == REVIEW_LANES[0] else [],
        }
        for lane_id in REVIEW_LANES
    ]
    dispositions: dict[str, object] = {
        "cross_lane_interaction_complete": True,
        "adjacent_variant_complete": True,
        "items": [
            {
                "candidate_id": f"candidate-{index}",
                "disposition": "emitted_finding",
                "finding_id": f"finding-{index}",
                "reason": "Verified",
            }
            for index in range(1, candidate_count + 1)
        ],
    }
    entries = derive_manifest_entries(document, {})
    shadow: dict[str, object] = {
        "status": "valid",
        "routing_decisions": {},
        "manifest_entries": entries,
        "manifest_digest": manifest_digest(entries),
        "entry_count": len(entries),
    }
    return document, lanes, dispositions, shadow


def _validate(
    tmp_path: Path,
    document: PlanDocument,
    lanes: list[object],
    dispositions: dict[str, object],
    shadow: dict[str, object],
) -> dict[str, object]:
    return validate_review_coverage(
        evidence_id="evidence-1",
        project_root=tmp_path,
        document=document,
        plan_hash="a" * 64,
        lane_results=lanes,
        candidate_dispositions=dispositions,
        shadow_manifest_status=shadow,
        expected_shadow_manifest_status=shadow,
    )


def test_valid_coverage_returns_canonical_attestation(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)

    attestation = _validate(tmp_path, document, lanes, dispositions, shadow)

    attested_lanes = attestation["lanes"]
    assert isinstance(attested_lanes, list)
    assert [lane["lane_id"] for lane in attested_lanes] == list(REVIEW_LANES)
    assert [lane["status"] for lane in attested_lanes] == [
        "completed",
        "delegated-verified",
        "completed",
    ]
    assert attestation["disposition_counts"] == {
        "total": 1,
        "emitted_findings": 1,
        "dismissed": 0,
    }
    assert validate_coverage_attestation(attestation, verdict="approved") == attestation


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "incomplete", "repository-not-delegated"],
)
def test_coverage_rejects_missing_duplicate_or_incomplete_lanes(
    tmp_path: Path,
    mutation: str,
) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    if mutation == "missing":
        lanes.pop()
    elif mutation == "duplicate":
        lanes[-1] = copy.deepcopy(lanes[0])
    elif mutation == "incomplete":
        assert isinstance(lanes[-1], dict)
        lanes[-1]["status"] = "failed"
    else:
        assert isinstance(lanes[1], dict)
        lanes[1]["status"] = "completed"

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "invalid_lane_results"


def test_coverage_rejects_invalid_section_ids(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    assert isinstance(lanes[0], dict)
    lanes[0]["section_ids_checked"] = ["missing"]

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "invalid_section_ids"


def test_coverage_rejects_undisposed_candidates(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    dispositions["items"] = []

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "undisposed_candidates"


def test_coverage_rejects_duplicate_finding_ids(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path, candidate_count=2)
    items = dispositions["items"]
    assert isinstance(items, list)
    assert isinstance(items[1], dict)
    items[1]["finding_id"] = "finding-1"

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "duplicate_finding"


def test_coverage_rejects_path_escape(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    for lane in lanes:
        assert isinstance(lane, dict)
        citations = lane["source_citations"]
        assert isinstance(citations, list)
        assert isinstance(citations[0], dict)
        citations[0]["path"] = f"../{outside.name}"

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "invalid_source_path"


@pytest.mark.parametrize("mutation", ["changed", "missing"])
def test_coverage_reports_source_drift(tmp_path: Path, mutation: str) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    if mutation == "changed":
        (tmp_path / "src" / "example.py").write_text("VALUE = 2\n", encoding="utf-8")
    else:
        for lane in lanes:
            assert isinstance(lane, dict)
            citations = lane["source_citations"]
            assert isinstance(citations, list)
            assert isinstance(citations[0], dict)
            citations[0]["path"] = "src/missing.py"

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "source_drift"
    assert error.value.retryable is True


def test_coverage_rejects_shadow_manifest_mismatch(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    supplied = copy.deepcopy(shadow)
    supplied["manifest_digest"] = "f" * 64

    with pytest.raises(ReviewEvidenceError) as error:
        validate_review_coverage(
            evidence_id="evidence-1",
            project_root=tmp_path,
            document=document,
            plan_hash="a" * 64,
            lane_results=lanes,
            candidate_dispositions=dispositions,
            shadow_manifest_status=supplied,
            expected_shadow_manifest_status=shadow,
        )

    assert error.value.code == "shadow_manifest_mismatch"


def test_approval_rejects_invalid_shadow_manifest() -> None:
    attestation = coverage_attestation(
        evidence_id="evidence-1",
        shadow_valid=False,
    )

    with pytest.raises(ReviewEvidenceError, match="valid shadow manifest"):
        validate_coverage_attestation(attestation, verdict="approved")


def test_attestation_rejects_completed_repository_lane() -> None:
    attestation = coverage_attestation(evidence_id="evidence-1")
    lanes = attestation["lanes"]
    assert isinstance(lanes, list)
    assert isinstance(lanes[1], dict)
    lanes[1]["status"] = "completed"

    with pytest.raises(ReviewEvidenceError, match="canonical lane statuses"):
        validate_coverage_attestation(attestation, verdict="needs_review")
