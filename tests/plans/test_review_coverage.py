from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.plans.review_evidence import register_review_evidence_tools
from gobby.plans.manifest_emitter import derive_manifest_entries
from gobby.plans.parser import Kind, PlanDocument, parse_plan
from gobby.plans.review_coverage import (
    REVIEW_LANES,
    review_complexity,
    review_coverage_input_schema,
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


@pytest.mark.parametrize(
    "mutation",
    [
        {"manifest_digest": "f" * 64},
        {"ok": True, "manifest_digest": "f" * 64},
        {"ok": True, "unexpected": "value"},
        {"ok": False},
    ],
)
def test_coverage_rejects_shadow_manifest_mismatch(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    supplied = {**copy.deepcopy(shadow), **mutation}

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


def _node(root: object, *path: str) -> dict[str, object]:
    """Walk the published schema, asserting each hop is an object."""
    current = root
    for key in path:
        assert isinstance(current, dict), f"{key!r} is not reachable through an object"
        current = current[key]
    assert isinstance(current, dict)
    return current


def _enum(root: object, *path: str) -> list[str]:
    values = _node(root, *path)["enum"]
    assert isinstance(values, list)
    assert all(isinstance(value, str) for value in values)
    return [str(value) for value in values]


def _lane(lanes: list[object], index: int) -> dict[str, object]:
    lane = lanes[index]
    assert isinstance(lane, dict)
    return lane


def _candidate(lanes: list[object], candidate_index: int) -> dict[str, object]:
    """Read one candidate issue; _coverage_case hangs them all off the first lane."""
    candidates = _lane(lanes, 0)["candidate_issues"]
    assert isinstance(candidates, list)
    candidate = candidates[candidate_index]
    assert isinstance(candidate, dict)
    return candidate


def _items(dispositions: dict[str, object]) -> list[dict[str, object]]:
    items = dispositions["items"]
    assert isinstance(items, list)
    assert all(isinstance(item, dict) for item in items)
    return [dict(item) for item in items]


def _codes(error: ReviewEvidenceError) -> list[str]:
    return [str(entry["error"]) for entry in error.errors]


_LANE_SCHEMA_PATH = ("lane_results", "items", "properties")
_CANDIDATE_SCHEMA_PATH = (*_LANE_SCHEMA_PATH, "candidate_issues", "items")


def test_published_lane_shapes_describe_an_accepted_payload(tmp_path: Path) -> None:
    """Lane ids, statuses and citation shape come from the constants the validator uses."""
    schema = review_coverage_input_schema()
    lane_results = _node(schema, "lane_results")
    lane_properties = _node(schema, *_LANE_SCHEMA_PATH)
    status_description = _node(lane_properties, "status")["description"]
    assert isinstance(status_description, str)
    assert lane_results["minItems"] == len(REVIEW_LANES)
    assert lane_results["maxItems"] == len(REVIEW_LANES)
    assert _enum(lane_properties, "lane_id") == list(REVIEW_LANES)

    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    _validate(tmp_path, document, lanes, dispositions, shadow)

    accepted = [_lane(lanes, index) for index in range(len(lanes))]
    assert [str(lane["lane_id"]) for lane in accepted] == _enum(lane_properties, "lane_id")
    for lane in accepted:
        assert str(lane["status"]) in _enum(lane_properties, "status")
        assert f"{lane['lane_id']}={lane['status']}" in status_description

    citation_schema = _node(lane_properties, "source_citations", "items")
    assert _node(lane_properties, "source_citations")["minItems"] == 1
    assert citation_schema["required"] == ["path", "sha256"]
    citations = accepted[0]["source_citations"]
    assert isinstance(citations, list)
    assert set(_node(citations[0])) <= set(_node(citation_schema, "properties"))
    assert _node(lane_properties, "section_ids_checked")["type"] == "array"


def test_published_candidate_properties_are_the_validator_closed_set(tmp_path: Path) -> None:
    """The published property names are the same closed set the validator allows."""
    candidate_schema = _node(review_coverage_input_schema(), *_CANDIDATE_SCHEMA_PATH)
    published = _node(candidate_schema, "properties")
    assert candidate_schema["required"] == list(published)

    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    assert set(_candidate(lanes, 0)) == set(published)
    _validate(tmp_path, document, lanes, dispositions, shadow)

    _candidate(lanes, 0)["unpublished_field"] = "value"
    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "invalid_candidate"
    assert "unpublished_field" in str(error.value)


def test_published_confidence_bounds_are_the_enforced_bounds(tmp_path: Path) -> None:
    """Both published bounds are accepted; a value past the maximum is not."""
    confidence = _node(review_coverage_input_schema(), *_CANDIDATE_SCHEMA_PATH, "properties")
    minimum = _node(confidence, "confidence")["minimum"]
    maximum = _node(confidence, "confidence")["maximum"]
    assert isinstance(minimum, int | float)
    assert isinstance(maximum, int | float)

    document, lanes, dispositions, shadow = _coverage_case(tmp_path, candidate_count=2)
    _candidate(lanes, 0)["confidence"] = minimum
    _candidate(lanes, 1)["confidence"] = maximum
    _validate(tmp_path, document, lanes, dispositions, shadow)

    _candidate(lanes, 1)["confidence"] = float(maximum) + 0.5
    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "invalid_candidate"
    assert "between 0 and 1" in str(error.value)


def test_published_disposition_and_shadow_shapes_describe_an_accepted_payload(
    tmp_path: Path,
) -> None:
    """Both disposition values and the shadow manifest's routing_decisions are published."""
    schema = review_coverage_input_schema()
    wrapper = _node(schema, "candidate_dispositions")
    published_dispositions = _enum(
        wrapper, "properties", "items", "items", "properties", "disposition"
    )
    shadow_schema = _node(schema, "shadow_manifest_status")
    assert wrapper["required"] == [
        "cross_lane_interaction_complete",
        "adjacent_variant_complete",
        "items",
    ]
    assert shadow_schema["required"] == ["status", "routing_decisions"]
    assert _node(shadow_schema, "properties", "routing_decisions")["type"] == "object"
    assert "manifest_entries" in _node(shadow_schema, "properties")

    document, lanes, dispositions, shadow = _coverage_case(tmp_path, candidate_count=2)
    dismissed = _items(dispositions)
    dismissed[1] = {
        "candidate_id": "candidate-2",
        "disposition": published_dispositions[1],
        "reason": "Not a defect",
    }
    dispositions["items"] = dismissed
    attestation = _validate(tmp_path, document, lanes, dispositions, shadow)

    assert {str(item["disposition"]) for item in dismissed} == set(published_dispositions)
    assert attestation["disposition_counts"] == {
        "total": 2,
        "emitted_findings": 1,
        "dismissed": 1,
    }
    assert str(shadow["status"]) in _enum(shadow_schema, "properties", "status")
    required = shadow_schema["required"]
    assert isinstance(required, list)
    assert set(required) <= set(shadow)


def test_lane_arm_reports_every_independent_error_at_once(tmp_path: Path) -> None:
    """Four unrelated lane defects come back in one response, not one per submission."""
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    _lane(lanes, 0)["source_citations"] = []
    _candidate(lanes, 0)["confidence"] = 7
    _lane(lanes, 1)["status"] = "completed"
    _lane(lanes, 2)["section_ids_checked"] = []

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert _codes(error.value) == [
        "invalid_source_citation",
        "invalid_candidate",
        "invalid_lane_results",
        "invalid_section_ids",
    ]
    payload = error.value.to_dict()
    assert payload["error"] == "invalid_source_citation"
    assert payload["errors"] == error.value.errors
    assert "review lane runtime_invariants" in str(error.value.errors[3]["message"])


def test_disposition_arm_reports_every_independent_error_at_once(tmp_path: Path) -> None:
    """The disposition arm collects across the wrapper flags and every item."""
    document, lanes, dispositions, shadow = _coverage_case(tmp_path, candidate_count=4)
    dispositions["adjacent_variant_complete"] = False
    items = _items(dispositions)
    items[0]["disposition"] = "maybe"
    items[1]["finding_id"] = "finding-dup"
    items[2]["finding_id"] = "finding-dup"
    items[3]["reason"] = ""
    dispositions["items"] = items

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert _codes(error.value) == [
        "incomplete_dispositions",
        "invalid_dispositions",
        "duplicate_finding",
        "invalid_candidate_disposition",
    ]
    assert error.value.to_dict()["error"] == "incomplete_dispositions"


def test_a_non_array_items_reports_only_its_own_error(tmp_path: Path) -> None:
    """A non-array items already explains the missing dispositions; it reports once."""
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    dispositions["items"] = "not-an-array"

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert _codes(error.value) == ["invalid_dispositions"]
    assert "must be an array" in str(error.value)


def test_a_single_failure_still_reports_one_entry(tmp_path: Path) -> None:
    """Every ReviewEvidenceError carries an errors list, single failure included."""
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    _lane(lanes, 1)["status"] = "completed"

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.errors == [
        {
            "error": "invalid_lane_results",
            "message": "review lane repository_blast_radius has a non-canonical status",
        }
    ]


def test_registered_tool_schema_carries_the_derived_shapes() -> None:
    """The MCP registration publishes the derived shapes, not bare objects."""
    registry = InternalToolRegistry(name="test-review-coverage")
    with patch(
        "gobby.mcp_proxy.tools.plans.review_evidence.PlanReviewEvidenceService",
        return_value=MagicMock(),
    ):
        register_review_evidence_tools(
            registry,
            MagicMock(),
            resolve_project_id=lambda _project: "project-1",
        )

    schema = registry.get_schema("validate_plan_review_coverage")
    assert schema is not None
    registered = _node(schema, "inputSchema")
    published = review_coverage_input_schema()
    assert _node(registered, "properties", "evidence_id")["type"] == "string"
    for name, shape in published.items():
        assert _node(registered, "properties", name) == shape
    assert registered["required"] == [
        "evidence_id",
        "lane_results",
        "candidate_dispositions",
        "shadow_manifest_status",
    ]
