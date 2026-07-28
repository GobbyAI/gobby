from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
import yaml

from gobby.plans.manifest_emitter import derive_manifest_entries
from gobby.plans.parser import Kind, PlanDocument, parse_plan
from gobby.plans.review_coverage import (
    REVIEW_LANES,
    review_complexity,
    validate_approval_condition,
    validate_coverage_attestation,
    validate_review_coverage,
)
from gobby.plans.review_evidence_io import build_section_manifest
from gobby.plans.review_evidence_models import ReviewEvidenceError, validate_round_result
from gobby.plans.review_ledger import inject_dismissed_ledger_context
from tests.review_coverage_helpers import coverage_attestation, manifest_digest

ROOT = Path(__file__).resolve().parents[2]
PLAN_SKILL = ROOT / "src/gobby/install/shared/skills/plan/SKILL.md"
PLAN_REVIEW_SKILL = ROOT / "src/gobby/install/shared/skills/plan-review/SKILL.md"
ADVERSARY_DIR = ROOT / "src/gobby/install/shared/workflows/agents"


def test_lane_verifier_invocation_and_allowlist() -> None:
    plan = PLAN_SKILL.read_text(encoding="utf-8")
    review = PLAN_REVIEW_SKILL.read_text(encoding="utf-8")

    assert "Do not run a separate pre-spawn `gcode index`" in plan
    assert review.count("verify_plan_review_index_token(index_token)") >= 2
    assert "immediately before analysis" in review
    assert "after its final repository search" in review
    assert "--no-freshness" in review
    assert "protocol implementors" in review
    assert "rerun any lane in place" in review

    for filename in ("plan-adversary-taskless.yaml", "plan-adversary.yaml"):
        definition = yaml.safe_load((ADVERSARY_DIR / filename).read_text(encoding="utf-8"))
        instructions = definition["instructions"]
        review_step = next(step for step in definition["steps"] if step["name"] == "review")
        allowed = set(review_step["allowed_mcp_tools"])

        assert "gobby-plans:verify_plan_review_index_token" in allowed
        assert all(not tool.endswith(":*") for tool in allowed)
        assert instructions.count("verify_plan_review_index_token(index_token)") >= 2
        assert "--no-freshness" in instructions
        assert "immediately before analysis" in instructions
        assert "after its final repository search" in instructions
        assert "inconclusive" in instructions
        assert "index_mismatch" in instructions
        assert "terminate" in instructions


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
    source.parent.mkdir(exist_ok=True)
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
            "status": "completed",
            "section_ids_checked": section_ids,
            "source_citations": [citation],
            "candidate_issues": candidates if lane_id == REVIEW_LANES[0] else [],
        }
        for lane_id in REVIEW_LANES
    ]
    dispositions: dict[str, object] = {
        "cross_lane_interactions": [],
        "adjacent_variant_sweeps": [
            {
                "check_key": "candidate-parity",
                "seed_candidate_id": f"candidate-{index}",
                "query_evidence": [f"gcode search candidate-{index}"],
                "sites_checked": ["src/adjacent.py"],
                "resulting_candidate_ids": [],
            }
            for index in range(1, candidate_count + 1)
        ],
        "causal_repair_sweeps": [],
        "candidate_dispositions": [
            {
                "candidate_id": f"candidate-{index}",
                "check_key": "candidate-parity",
                "source_section_ids": [section_ids[0]],
                "source_hash": citation["sha256"],
                "disposition": "emitted_finding",
                "finding_id": f"finding-{index}",
                "rationale": "Verified",
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
    prior_round_context: dict[str, object] | None = None,
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
        prior_round_context=prior_round_context,
    )


def test_valid_coverage_returns_canonical_attestation(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)

    attestation = _validate(tmp_path, document, lanes, dispositions, shadow)

    attested_lanes = attestation["lanes"]
    assert isinstance(attested_lanes, list)
    assert [lane["lane_id"] for lane in attested_lanes] == list(REVIEW_LANES)
    assert attestation["disposition_counts"] == {
        "total": 1,
        "emitted_findings": 1,
        "dismissed": 0,
    }
    assert validate_coverage_attestation(attestation, verdict="approved") == attestation


def test_derived_sweep_booleans(tmp_path: Path) -> None:
    document, lanes, records, shadow = _coverage_case(tmp_path, candidate_count=2)
    first_lane = lanes[0]
    second_lane = lanes[1]
    assert isinstance(first_lane, dict)
    assert isinstance(second_lane, dict)
    candidates = first_lane["candidate_issues"]
    assert isinstance(candidates, list)
    first_lane["candidate_issues"] = candidates[:1]
    second_lane["candidate_issues"] = candidates[1:]
    records["cross_lane_interactions"] = [
        {
            "candidate_ids": ["candidate-1", "candidate-2"],
            "affected_section_ids": ["1.1"],
            "interaction_checked": "Conflicting parity repairs",
            "disposition": "independent",
        }
    ]

    complete = _validate(tmp_path, document, lanes, records, shadow)
    assert complete["cross_lane_interaction_complete"] is True
    assert complete["adjacent_variant_complete"] is True

    records["cross_lane_interactions"] = []
    partial = _validate(tmp_path, document, lanes, records, shadow)
    assert partial["cross_lane_interaction_complete"] is False
    assert partial["adjacent_variant_complete"] is True


def test_unreferenced_candidate_rejected(tmp_path: Path) -> None:
    document, lanes, records, shadow = _coverage_case(tmp_path)
    records["adjacent_variant_sweeps"] = []

    with pytest.raises(ReviewEvidenceError, match="candidate-1") as error:
        _validate(tmp_path, document, lanes, records, shadow)

    assert error.value.code == "unreferenced_candidate"


def test_sweep_universe_fixtures(tmp_path: Path) -> None:
    empty_document, empty_lanes, empty_records, empty_shadow = _coverage_case(
        tmp_path,
        candidate_count=0,
    )
    empty = _validate(
        tmp_path,
        empty_document,
        empty_lanes,
        empty_records,
        empty_shadow,
    )
    assert empty["cross_lane_interaction_complete"] is True
    assert empty["adjacent_variant_complete"] is True

    document, lanes, records, shadow = _coverage_case(tmp_path)
    assert _validate(tmp_path, document, lanes, records, shadow)
    adjacent = records["adjacent_variant_sweeps"]
    assert isinstance(adjacent, list)
    assert isinstance(adjacent[0], dict)
    adjacent[0]["query_evidence"] = []
    with pytest.raises(ReviewEvidenceError, match="query evidence"):
        _validate(tmp_path, document, lanes, records, shadow)

    adjacent[0]["query_evidence"] = ["gcode search candidate-1"]
    adjacent.append(
        {
            "check_key": "extra-check",
            "seed_candidate_id": "candidate-1",
            "query_evidence": ["gcode search extra-check"],
            "sites_checked": [],
            "resulting_candidate_ids": [],
        }
    )
    with pytest.raises(ReviewEvidenceError, match="outside the required universe"):
        _validate(tmp_path, document, lanes, records, shadow)

    adjacent.pop()
    prior_context = {
        "prior_finding_resolutions": [{"prior_finding_id": "finding-prior", "decision": "repair"}],
        "repair_attestations": [
            {
                "prior_finding_id": "finding-prior",
                "changed_section_ids": ["1.1"],
            }
        ],
        "consumer_site_inventory": {
            "changed_contracts": ["contracts/review.json"],
            "sites": [{"site_id": "consumer-1"}],
        },
        "dismissed_ledger_entries": [],
    }
    with pytest.raises(ReviewEvidenceError, match="finding-prior"):
        _validate(
            tmp_path,
            document,
            lanes,
            records,
            shadow,
            prior_context,
        )
    records["causal_repair_sweeps"] = [
        {
            "prior_finding_id": "finding-prior",
            "changed_section_ids": ["1.1"],
            "changed_contracts": ["contracts/review.json"],
            "sites_checked": ["consumer-1"],
            "query_evidence": [],
            "disposition": "validated",
        }
    ]
    assert _validate(
        tmp_path,
        document,
        lanes,
        records,
        shadow,
        prior_context,
    )


def test_dispositions_reconcile_with_counts(tmp_path: Path) -> None:
    document, lanes, records, shadow = _coverage_case(tmp_path)
    attestation = _validate(tmp_path, document, lanes, records, shadow)
    attestation["disposition_counts"] = {
        "total": 0,
        "emitted_findings": 0,
        "dismissed": 0,
    }

    with pytest.raises(ReviewEvidenceError, match="disposition_counts"):
        validate_coverage_attestation(attestation, verdict="needs_review")


def test_validator_returns_canonical_record_bundle(tmp_path: Path) -> None:
    document, lanes, records, shadow = _coverage_case(tmp_path)
    disposition_items = records["candidate_dispositions"]
    assert isinstance(disposition_items, list)
    assert isinstance(disposition_items[0], dict)
    disposition_items[0]["disposition"] = "dismissed"
    disposition_items[0].pop("finding_id")

    attestation = _validate(tmp_path, document, lanes, records, shadow)
    expected_bundle = {
        "cross_lane_interactions": records["cross_lane_interactions"],
        "adjacent_variant_sweeps": records["adjacent_variant_sweeps"],
        "causal_repair_sweeps": records["causal_repair_sweeps"],
        "candidate_dispositions": records["candidate_dispositions"],
    }
    assert attestation["record_bundle"] == expected_bundle

    round_result = validate_round_result(
        {
            "verdict": "needs_review",
            "findings": [],
            "coverage_attestation": attestation,
        }
    )
    round_attestation = round_result["coverage_attestation"]
    assert isinstance(round_attestation, dict)
    assert round_attestation["record_bundle"] == expected_bundle

    dropped = copy.deepcopy(attestation)
    dropped.pop("record_bundle")
    with pytest.raises(ReviewEvidenceError, match="record_bundle"):
        validate_round_result(
            {
                "verdict": "needs_review",
                "findings": [],
                "coverage_attestation": dropped,
            }
        )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "incomplete"])
def test_coverage_rejects_missing_duplicate_or_incomplete_lanes(
    tmp_path: Path,
    mutation: str,
) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    if mutation == "missing":
        lanes.pop()
    elif mutation == "duplicate":
        lanes[-1] = copy.deepcopy(lanes[0])
    else:
        assert isinstance(lanes[-1], dict)
        lanes[-1]["status"] = "failed"

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "invalid_lane_results"


def test_lanes_still_cover_all_sections(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    assert isinstance(lanes[0], dict)
    section_ids = lanes[0]["section_ids_checked"]
    assert isinstance(section_ids, list)
    lanes[0]["section_ids_checked"] = section_ids[:-1]

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "invalid_section_ids"


def test_coverage_rejects_undisposed_candidates(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    dispositions["candidate_dispositions"] = []

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(tmp_path, document, lanes, dispositions, shadow)

    assert error.value.code == "undisposed_candidates"


def test_unchanged_dismissal_reopen_rejected(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path)
    items = dispositions["candidate_dispositions"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    current_hashes = {
        section.section_id: section.section_hash
        for section in build_section_manifest(document.source_path.read_bytes())
    }

    def context(*, source_hash: str, section_hash: str) -> dict[str, object]:
        return inject_dismissed_ledger_context(
            prior_round_context={"prior_evidence_id": "evidence-prior"},
            prior_ledger=[
                {
                    "ledger_entry_id": "ledger-" + "d" * 64,
                    "kind": "dismissed",
                    "check_key": item["check_key"],
                    "aliases": ["candidate-prior"],
                    "first_seen_round": 1,
                    "rounds_carried": 1,
                    "source_section_ids": item["source_section_ids"],
                    "section_hashes_at_entry": {"1.1": section_hash},
                    "stale": False,
                    "source_hash": source_hash,
                    "rationale": "Previously dismissed.",
                }
            ],
            current_section_hashes=current_hashes,
        )

    with pytest.raises(ReviewEvidenceError) as error:
        _validate(
            tmp_path,
            document,
            lanes,
            dispositions,
            shadow,
            context(
                source_hash=str(item["source_hash"]),
                section_hash=current_hashes["1.1"],
            ),
        )
    assert error.value.code == "unchanged_dismissal_reopened"

    assert _validate(
        tmp_path,
        document,
        lanes,
        dispositions,
        shadow,
        context(source_hash="b" * 64, section_hash=current_hashes["1.1"]),
    )
    assert _validate(
        tmp_path,
        document,
        lanes,
        dispositions,
        shadow,
        context(source_hash=str(item["source_hash"]), section_hash="b" * 64),
    )


def test_coverage_rejects_duplicate_finding_ids(tmp_path: Path) -> None:
    document, lanes, dispositions, shadow = _coverage_case(tmp_path, candidate_count=2)
    items = dispositions["candidate_dispositions"]
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
            prior_round_context=None,
        )

    assert error.value.code == "shadow_manifest_mismatch"


def test_approval_rejects_invalid_shadow_manifest() -> None:
    attestation = coverage_attestation(
        evidence_id="evidence-1",
        shadow_valid=False,
    )

    with pytest.raises(ReviewEvidenceError, match="valid shadow manifest"):
        validate_coverage_attestation(attestation, verdict="approved")


def test_approval_condition_blocking_only() -> None:
    ledger = [
        {
            "ledger_entry_id": f"ledger-{digit * 64}",
            "kind": "finding",
            "check_key": f"{severity}-quality-risk",
            "aliases": [f"{severity}-finding"],
            "first_seen_round": 1,
            "rounds_carried": 1,
            "source_section_ids": ["1.1"],
            "section_hashes_at_entry": {"1.1": "a" * 64},
            "stale": False,
            "category": "unhandled-edge",
            "severity": severity,
            "location": "§ 1.1",
            "description": f"{severity} non-gating risk",
            "minimal_repair": "Record the explicit quality decision.",
            "repair_scope": "existing_sections",
            "prevention": "Surface the quality ledger at approval.",
        }
        for severity, digit in (("major", "1"), ("minor", "2"))
    ]

    approved = validate_approval_condition(findings=[], quality_ledger=ledger)

    assert [entry["severity"] for entry in approved] == ["major", "minor"]
    with pytest.raises(ReviewEvidenceError) as blocked:
        validate_approval_condition(
            findings=[{"finding_id": "blocking-1", "severity": "blocking"}],
            quality_ledger=ledger,
        )
    assert blocked.value.code == "blocking_findings_remaining"
