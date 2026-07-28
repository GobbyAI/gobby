from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.code_index import IndexToken
from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.mcp_proxy.tools.plans import review_evidence as review_evidence_tools
from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.review_coverage import _validate_candidate
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import build_section_manifest
from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError
from gobby.plans.review_findings import validate_plan_review_findings
from gobby.plans.review_repair import (
    DEVIATION_PROOF_FIELDS,
    REPAIR_SUBMISSION_ARTIFACT_KEY,
    RepairUniverse,
    build_repair_submission,
    canonicalize_repair_submission,
    decode_repair_submission,
    derive_repair_universe,
    encode_repair_submission,
    validate_repair_preparation,
    validate_repair_universe_attestations,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from tests.review_coverage_helpers import coverage_attestation
from tests.review_telemetry_helpers import enriched_telemetry

pytestmark = pytest.mark.unit


@pytest.fixture
def repair_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> tuple[PlanReviewEvidenceService, str, Path]:
    project = LocalProjectManager(temp_db).create(
        name="review-repair",
        repo_path=str(tmp_path),
    )
    plan_path = _write_plan(tmp_path)
    return PlanReviewEvidenceService(temp_db), project.id, plan_path


def _write_plan(root: Path) -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    path = plan_dir / "review-repair.md"
    path.write_text(
        textwrap.dedent(
            """
            # Review Repair
            **Plan ID:** review-repair

            ## P1 Phase
            `kind: framing`

            ### 1.1 Work
            `kind: deliverable`

            Target: `src/example.py`

            **Acceptance:**
            - 1.1.1 — Behavior exists. test: `tests/test_example.py`

            ## Task Mapping
            `kind: framing`

            Pending.

            ## V1 Plan Changelog
            `kind: verification`

            No rounds yet.

            ## M1 Task Manifest
            `kind: manifest`

            ```yaml
            - title: Implement example
              source_section: '1.1'
              covers:
                - 1.1.1
              category: code
              implementation_domain: backend
              priority: 2
              task_type: feature
              tdd: false
              labels:
                - covers:review-repair:1.1:1.1.1
              description: Implement the example.
              validation_criteria: Example behavior is tested.
            ```
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


def _finding(
    finding_id: str,
    *,
    severity: str = "major",
    check_key: str | None = None,
) -> dict[str, object]:
    finding: dict[str, object] = {
        "finding_id": finding_id,
        "section_id": "1.1",
        "check_key": check_key or f"repair.{finding_id}",
        "severity": severity,
        "category": "unhandled-edge",
        "location": "1.1",
        "description": f"Repair {finding_id}.",
        "minimal_repair": f"Apply the minimal repair for {finding_id}.",
        "repair_scope": "existing_sections",
        "prevention": "Validate the repaired surface.",
        "principle": "Repairs require evidence.",
    }
    if severity == "blocking":
        finding["failure_trace"] = {
            "preconditions": "The plan is reviewed.",
            "action": "The missing behavior is exercised.",
            "wrong_outcome": "The plan omits the required behavior.",
            "violated_obligation": "Every blocking path must be covered.",
            "citation": [{"path": "src/example.py", "sha256": "a" * 64}],
        }
    return finding


def _finalize_prior_round(
    service: PlanReviewEvidenceService,
    project_id: str,
    plan_path: Path,
    findings: list[dict[str, object]],
    *,
    candidate_dispositions: list[dict[str, object]] | None = None,
) -> PlanReviewEvidence:
    task_id = _new_task_id(service, project_id)
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        task_id=task_id,
        stage="planning",
    )
    attestation = coverage_attestation(
        evidence_id=prepared.evidence_id,
        shadow_valid=False,
    )
    result: dict[str, object] = {
        "verdict": "needs_review",
        "findings": findings,
        "coverage_attestation": attestation,
        "convergence_telemetry": enriched_telemetry(),
    }
    if candidate_dispositions:
        lanes = attestation["lanes"]
        assert isinstance(lanes, list)
        assert isinstance(lanes[0], dict)
        lanes[0]["candidate_count"] = len(candidate_dispositions)
        attestation["disposition_counts"] = {
            "total": len(candidate_dispositions),
            "emitted_findings": sum(
                record["disposition"] == "emitted_finding" for record in candidate_dispositions
            ),
            "dismissed": sum(
                record["disposition"] == "dismissed" for record in candidate_dispositions
            ),
        }
        record_bundle = attestation["record_bundle"]
        assert isinstance(record_bundle, dict)
        record_bundle["candidate_dispositions"] = candidate_dispositions
        record_bundle["adjacent_variant_sweeps"] = [
            {
                "check_key": record["check_key"],
                "seed_candidate_id": record["candidate_id"],
                "query_evidence": [f"test query for {record['candidate_id']}"],
                "sites_checked": [],
                "resulting_candidate_ids": [],
            }
            for record in candidate_dispositions
        ]
        unsigned = {key: value for key, value in attestation.items() if key != "attestation_digest"}
        attestation["attestation_digest"] = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return service.finalize_plan_review_evidence(prepared.evidence_id, result)


def _mark_plan_repaired(plan_path: Path) -> None:
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(
            "Behavior exists.",
            "Behavior is repaired.",
        ),
        encoding="utf-8",
    )


def _resolution(finding_id: str, decision: str) -> dict[str, object]:
    return {"prior_finding_id": finding_id, "decision": decision}


def _attestation(
    finding: dict[str, object],
    *,
    changed_section_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "prior_finding_id": finding["finding_id"],
        "check_key": finding["check_key"],
        "changed_section_ids": changed_section_ids or ["1.1"],
        "accepted_resolution": finding["minimal_repair"],
        "deviation_from_minimal_repair": None,
        "changed_symbols": ["gobby.example.repaired_behavior"],
        "consumer_sites_swept": ["src/example.py:consumer"],
        "adjacent_variants_swept": ["src/example.py:adjacent"],
        "validation_evidence": ["pytest tests/test_example.py"],
        "deferred_sites": [],
    }


def _candidate_inventory(
    *site_ids: str,
    changed_targets: tuple[str, ...] = ("src/example.py",),
    changed_contracts: tuple[str, ...] = (),
) -> CandidateSiteInventory:
    return CandidateSiteInventory(
        changed_acceptance_item_ids=("1.1.1",),
        changed_targets=changed_targets,
        changed_symbols=("gobby.example.repaired_behavior",),
        changed_contracts=changed_contracts,
        resolved_languages=("python",),
        unsupported_targets=(),
        sites=tuple(
            CandidateSite(
                site_id=site_id,
                path=f"src/{site_id}.py",
                source_kind="symbol_call",
                source_ref="gobby.example.repaired_behavior",
                status="resolved",
                language="python",
            )
            for site_id in site_ids
        ),
    )


def _universe_attestation(
    finding: dict[str, object],
    universe: RepairUniverse,
) -> dict[str, object]:
    requirement = next(
        requirement
        for requirement in universe.requirements
        if requirement.prior_finding_id == finding["finding_id"]
    )
    interaction_records = [
        {
            "edge_id": edge_id,
            "disposition": "compatible",
            "validation_evidence": ["pytest tests/test_example.py"],
        }
        for edge_id in requirement.interaction_edge_ids
    ]
    attestation = _attestation(finding)
    attestation.update(
        {
            "repair_universe_digest": universe.digest,
            "consumer_sites_swept": list(requirement.required_consumer_site_ids),
            "adjacent_variants_swept": list(requirement.adjacent_variant_ids),
            "deferred_sites": [],
            "sweep_query_evidence": ["gcode usages gobby.example.repaired_behavior"],
            "repair_bundle_interactions": interaction_records,
        }
    )
    return attestation


def _deviation_proof() -> dict[str, str]:
    return {
        "violated_invariant": "Every consumer must use the revised contract.",
        "original_counterexample": "src/example.py still called the removed branch.",
        "how_alternative_closes_it": "The alternative updates the shared caller boundary.",
        "validation_evidence": "pytest tests/test_example.py::test_revised_contract",
        "accepted_risk": "none",
    }


def _submission_payload(
    finding: dict[str, object],
    deviation: object,
) -> dict[str, object]:
    attestation = _attestation(finding)
    attestation["deviation_from_minimal_repair"] = deviation
    return {
        "round_number": 2,
        "prior_finding_resolutions": [_resolution(str(finding["finding_id"]), "repair")],
        "repair_attestations": [attestation],
    }


def _prepare_round_two(
    service: PlanReviewEvidenceService,
    project_id: str,
    plan_path: Path,
    *,
    resolutions: list[dict[str, object]],
    attestations: list[dict[str, object]],
) -> PlanReviewEvidence:
    task_id = _new_task_id(service, project_id)
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        task_id=task_id,
        stage="planning",
        prior_finding_resolutions=resolutions,
        repair_attestations=attestations,
    )
    return service.get_evidence(prepared.evidence_id)


def _new_task_id(service: PlanReviewEvidenceService, project_id: str) -> str:
    task = LocalTaskManager(service.db).create_task(
        project_id=project_id,
        title="Plan review repair fixture",
        task_type="review_anchor",
        category="planning",
        validation_criteria="Plan review repair fixture remains attributable.",
    )
    return task.id


def test_unattested_finding_refuses_preparation(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    finding = _finding("finding-1")
    _finalize_prior_round(service, project_id, plan_path, [finding])
    _mark_plan_repaired(plan_path)

    with pytest.raises(ReviewEvidenceError, match="finding-1") as refused:
        _prepare_round_two(
            service,
            project_id,
            plan_path,
            resolutions=[_resolution("finding-1", "repair")],
            attestations=[],
        )

    assert refused.value.code == "missing_repair_attestation"


def test_attestation_must_match_hash_diff(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    finding = _finding("finding-1")
    _finalize_prior_round(service, project_id, plan_path, [finding])
    _mark_plan_repaired(plan_path)
    resolution = [_resolution("finding-1", "repair")]

    with pytest.raises(ReviewEvidenceError, match="changed_section_ids"):
        _prepare_round_two(
            service,
            project_id,
            plan_path,
            resolutions=resolution,
            attestations=[_attestation(finding, changed_section_ids=["P1"])],
        )

    duplicate = _attestation(finding)
    with pytest.raises(ReviewEvidenceError, match="duplicate.*finding-1"):
        _prepare_round_two(
            service,
            project_id,
            plan_path,
            resolutions=resolution,
            attestations=[duplicate, duplicate],
        )

    unknown = {**_attestation(finding), "prior_finding_id": "unknown"}
    with pytest.raises(ReviewEvidenceError, match="unknown"):
        _prepare_round_two(
            service,
            project_id,
            plan_path,
            resolutions=resolution,
            attestations=[unknown],
        )

    mismatch = {**_attestation(finding), "check_key": "repair.other"}
    with pytest.raises(ReviewEvidenceError, match="check_key"):
        _prepare_round_two(
            service,
            project_id,
            plan_path,
            resolutions=resolution,
            attestations=[mismatch],
        )


def test_mixed_repair_carry_preparation(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    repaired = _finding("repair-major")
    carried = _finding("carry-minor", severity="minor")
    blocking = _finding("repair-blocking", severity="blocking")
    findings = [repaired, carried, blocking]
    _finalize_prior_round(service, project_id, plan_path, findings)
    _mark_plan_repaired(plan_path)

    with pytest.raises(ReviewEvidenceError, match="blocking.*carry"):
        _prepare_round_two(
            service,
            project_id,
            plan_path,
            resolutions=[
                _resolution("repair-major", "repair"),
                _resolution("carry-minor", "carry"),
                _resolution("repair-blocking", "carry"),
            ],
            attestations=[_attestation(repaired)],
        )

    resolutions = [
        _resolution("repair-major", "repair"),
        _resolution("carry-minor", "carry"),
        _resolution("repair-blocking", "repair"),
    ]
    attestations = [_attestation(repaired), _attestation(blocking)]
    current = _prepare_round_two(
        service,
        project_id,
        plan_path,
        resolutions=resolutions,
        attestations=attestations,
    )

    assert current.repair_attestations == attestations
    context = current.prior_round_context
    assert context is not None
    assert context == {
        "requirements_bundle": context["requirements_bundle"],
        "prior_evidence_id": context["prior_evidence_id"],
        "prior_findings": [
            {
                "finding_id": repaired["finding_id"],
                "check_key": repaired["check_key"],
            },
            {
                "finding_id": carried["finding_id"],
                "check_key": carried["check_key"],
            },
            {
                "finding_id": blocking["finding_id"],
                "check_key": blocking["check_key"],
            },
        ],
        "prior_finding_resolutions": resolutions,
        "repair_attestations": attestations,
        "changed_acceptance_item_ids": ["1.1.1"],
        "changed_section_targets": [],
        "dismissed_ledger_entries": [],
    }


def test_prior_round_context_structure(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    finding = _finding("finding-1", check_key="behavior.repaired")
    _finalize_prior_round(service, project_id, plan_path, [finding])
    _mark_plan_repaired(plan_path)
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(
            "Target: `src/example.py`",
            "Target: `src/repaired.py`",
        ),
        encoding="utf-8",
    )
    resolutions = [_resolution("finding-1", "repair")]
    attestations = [_attestation(finding)]

    current = _prepare_round_two(
        service,
        project_id,
        plan_path,
        resolutions=resolutions,
        attestations=attestations,
    )

    context = current.prior_round_context
    assert context is not None
    expected = {
        "requirements_bundle": context["requirements_bundle"],
        "prior_evidence_id": context["prior_evidence_id"],
        "prior_findings": [
            {
                "finding_id": "finding-1",
                "check_key": "behavior.repaired",
            },
        ],
        "prior_finding_resolutions": resolutions,
        "repair_attestations": attestations,
        "changed_acceptance_item_ids": ["1.1.1"],
        "changed_section_targets": ["src/example.py", "src/repaired.py"],
        "dismissed_ledger_entries": [],
    }
    assert context == expected
    assert service.snapshot_payload(current.evidence_id)["prior_round_context"] == expected


def test_preparation_injects_persisted_dismissal(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    prior = _finalize_prior_round(
        service,
        project_id,
        plan_path,
        [],
        candidate_dispositions=[
            {
                "candidate_id": "candidate-prior",
                "check_key": "candidate-parity",
                "source_section_ids": ["1.1"],
                "source_hash": "c" * 64,
                "disposition": "dismissed",
                "rationale": "Previously satisfied by the existing invariant.",
            }
        ],
    )

    current = _prepare_round_two(
        service,
        project_id,
        plan_path,
        resolutions=[],
        attestations=[],
    )

    context = current.prior_round_context
    assert context is not None
    assert prior.quality_ledger is not None
    entries = context["dismissed_ledger_entries"]
    assert isinstance(entries, list)
    assert entries == [
        {
            **prior.quality_ledger[0],
            "reopenable": False,
        }
    ]
    assert service.snapshot_payload(current.evidence_id)["prior_round_context"] == context


def test_omitted_resolution_record_refuses(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    first = _finding("finding-1")
    second = _finding("finding-2", severity="minor")
    _finalize_prior_round(service, project_id, plan_path, [first, second])
    _mark_plan_repaired(plan_path)

    with pytest.raises(ReviewEvidenceError, match="finding-2") as refused:
        _prepare_round_two(
            service,
            project_id,
            plan_path,
            resolutions=[_resolution("finding-1", "repair")],
            attestations=[_attestation(first)],
        )

    assert refused.value.code == "missing_finding_resolution"


def test_remedy_vocabulary_round_trip(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    candidate = {
        "candidate_id": "candidate-1",
        "violated_invariant": "Consumer coverage is complete.",
        "suggested_fix": "Sweep every consumer.",
        "section_ids": ["1.1"],
        "confidence": 0.9,
        "source_citations": [{"path": "src/example.py", "sha256": "b" * 64}],
        "adjacent_sites_checked": [],
    }
    canonical_candidate = _validate_candidate(
        candidate,
        expected_sections={"1.1"},
        requirements_bundle=None,
    )
    finding = _finding("finding-1")
    finding["minimal_repair"] = canonical_candidate["suggested_fix"]
    prior = _finalize_prior_round(service, project_id, plan_path, [finding])

    canonical_finding = validate_plan_review_findings([finding], evidence=prior)[0]
    submission = build_repair_submission(
        round_number=2,
        prior_findings=[canonical_finding],
        recorded_votes=[
            {
                "prior_finding_id": "finding-1",
                "decision": "repair",
                "accepted_resolution": canonical_finding["minimal_repair"],
            }
        ],
        edit_diff={"finding-1": _attestation(canonical_finding)},
    )
    attestation = submission.repair_attestations[0]

    assert candidate["suggested_fix"] == finding["minimal_repair"]
    assert attestation["deviation_from_minimal_repair"] is None
    for forbidden in ("minimal_repair", "deviation_from_minimal_repair"):
        with pytest.raises(ReviewEvidenceError, match="unknown fields"):
            _validate_candidate(
                {**candidate, forbidden: "wrong"},
                expected_sections={"1.1"},
                requirements_bundle=None,
            )
    for forbidden in ("suggested_fix", "deviation_from_minimal_repair"):
        with pytest.raises(ReviewEvidenceError, match="unknown fields"):
            validate_plan_review_findings(
                [{**finding, forbidden: "wrong"}],
                evidence=prior,
            )
    for forbidden in ("suggested_fix", "minimal_repair"):
        with pytest.raises(ReviewEvidenceError, match="unknown fields"):
            build_repair_submission(
                round_number=2,
                prior_findings=[canonical_finding],
                recorded_votes=[
                    {
                        "prior_finding_id": "finding-1",
                        "decision": "repair",
                        "accepted_resolution": canonical_finding["minimal_repair"],
                    }
                ],
                edit_diff={
                    "finding-1": {
                        **_attestation(canonical_finding),
                        forbidden: "wrong",
                    }
                },
            )


def test_deviation_requires_proof() -> None:
    finding = _finding("finding-1")
    for missing_field in (
        "violated_invariant",
        "original_counterexample",
        "how_alternative_closes_it",
    ):
        incomplete = _deviation_proof()
        incomplete.pop(missing_field)
        with pytest.raises(ReviewEvidenceError, match="deviation_from_minimal_repair"):
            canonicalize_repair_submission(_submission_payload(finding, incomplete))

    submission = canonicalize_repair_submission(_submission_payload(finding, _deviation_proof()))
    assert submission.repair_attestations[0]["deviation_from_minimal_repair"] == (
        _deviation_proof()
    )


def test_deviation_counterexample_and_risk() -> None:
    finding = _finding("finding-1")
    for missing_field in ("validation_evidence", "accepted_risk"):
        incomplete = _deviation_proof()
        incomplete.pop(missing_field)
        with pytest.raises(ReviewEvidenceError, match="deviation_from_minimal_repair"):
            canonicalize_repair_submission(_submission_payload(finding, incomplete))

    for empty_field in ("validation_evidence", "accepted_risk"):
        incomplete = _deviation_proof()
        incomplete[empty_field] = " "
        with pytest.raises(ReviewEvidenceError, match=empty_field):
            canonicalize_repair_submission(_submission_payload(finding, incomplete))

    accepted = canonicalize_repair_submission(_submission_payload(finding, _deviation_proof()))
    deviation = accepted.repair_attestations[0]["deviation_from_minimal_repair"]
    assert isinstance(deviation, dict)
    assert deviation["accepted_risk"] == "none"


def test_deviation_schema_parity_across_surfaces(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    finding = _finding("finding-1")
    _finalize_prior_round(service, project_id, plan_path, [finding])
    _mark_plan_repaired(plan_path)
    proof = _deviation_proof()

    assert DEVIATION_PROOF_FIELDS == (
        "violated_invariant",
        "original_counterexample",
        "how_alternative_closes_it",
        "validation_evidence",
        "accepted_risk",
    )

    for malformed in (
        {**proof, "extra": "closed schemas reject this"},
        {key: value for key, value in proof.items() if key != "accepted_risk"},
    ):
        raw = _submission_payload(finding, malformed)
        resolutions = raw["prior_finding_resolutions"]
        attestations = raw["repair_attestations"]
        assert isinstance(resolutions, list)
        assert isinstance(attestations, list)
        with pytest.raises(ReviewEvidenceError, match="deviation_from_minimal_repair"):
            canonicalize_repair_submission(raw)
        with pytest.raises(ReviewEvidenceError, match="deviation_from_minimal_repair"):
            encode_repair_submission(raw)
        with pytest.raises(ReviewEvidenceError, match="deviation_from_minimal_repair"):
            decode_repair_submission(json.dumps(raw), expected_round_number=2)
        with pytest.raises(ReviewEvidenceError, match="deviation_from_minimal_repair"):
            _prepare_round_two(
                service,
                project_id,
                plan_path,
                resolutions=resolutions,
                attestations=attestations,
            )

    submission = build_repair_submission(
        round_number=2,
        prior_findings=[finding],
        recorded_votes=[
            {
                "prior_finding_id": "finding-1",
                "decision": "repair",
                "accepted_resolution": finding["minimal_repair"],
            }
        ],
        edit_diff={
            "finding-1": {
                **_attestation(finding),
                "deviation_from_minimal_repair": proof,
            }
        },
    )
    rendered = submission.to_dict()
    canonical = canonicalize_repair_submission(rendered)
    decoded = decode_repair_submission(
        encode_repair_submission(rendered),
        expected_round_number=2,
    )
    prepared = _prepare_round_two(
        service,
        project_id,
        plan_path,
        resolutions=list(submission.prior_finding_resolutions),
        attestations=list(submission.repair_attestations),
    )

    for surface in (submission, canonical, decoded):
        assert surface.repair_attestations[0]["deviation_from_minimal_repair"] == proof
    assert prepared.repair_attestations == list(submission.repair_attestations)


@pytest.mark.asyncio
async def test_taskless_producer_builds_records(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    repaired = _finding("finding-1")
    carried = _finding("finding-2", severity="minor")
    _finalize_prior_round(service, project_id, plan_path, [repaired, carried])
    _mark_plan_repaired(plan_path)
    votes: list[dict[str, object]] = [
        {
            "prior_finding_id": "finding-1",
            "decision": "repair",
            "accepted_resolution": repaired["minimal_repair"],
        },
        {"prior_finding_id": "finding-2", "decision": "carry"},
    ]
    submission = build_repair_submission(
        round_number=2,
        prior_findings=[repaired, carried],
        recorded_votes=votes,
        edit_diff={"finding-1": _attestation(repaired)},
    )

    assert submission.prior_finding_resolutions == (
        _resolution("finding-1", "repair"),
        _resolution("finding-2", "carry"),
    )
    with pytest.raises(ReviewEvidenceError, match="finding-2"):
        build_repair_submission(
            round_number=2,
            prior_findings=[repaired, carried],
            recorded_votes=votes[:1],
            edit_diff={"finding-1": _attestation(repaired)},
        )

    registry = create_plan_registry(service.db, default_project_id=project_id)
    task_id = _new_task_id(service, project_id)
    prepared = await registry.call(
        "prepare_plan_review_round",
        {
            "plan_path": str(plan_path),
            "round_number": 2,
            "task_id": task_id,
            "stage": "planning",
            "prior_finding_resolutions": list(submission.prior_finding_resolutions),
            "repair_attestations": list(submission.repair_attestations),
        },
    )

    assert prepared["ok"] is True
    stored = service.get_evidence(str(prepared["evidence_id"]))
    assert stored.repair_attestations == list(submission.repair_attestations)
    skill = Path("src/gobby/install/shared/skills/plan/SKILL.md").read_text(encoding="utf-8")
    assert "prior_finding_resolutions" in skill
    assert "repair_attestations" in skill


def test_staged_submission_payload_round_trip(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    finding = _finding("finding-1")
    submission = build_repair_submission(
        round_number=2,
        prior_findings=[finding],
        recorded_votes=[
            {
                "prior_finding_id": "finding-1",
                "decision": "repair",
                "accepted_resolution": finding["minimal_repair"],
            }
        ],
        edit_diff={"finding-1": _attestation(finding)},
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Persist repair submission",
        task_type="review_anchor",
        category="planning",
        validation_criteria="Repair submission persists through planning resubmission.",
    )
    manager.initialize_task_manifest(task.id, stage_names=["planning"])
    manager.stage_states.start_stage(task.id, "planning", by_session_id=None)
    manager.submit_for_review(
        task.id,
        "planning",
        repair_submission=submission.to_dict(),
    )

    restarted = LocalTaskManager(temp_db)
    stage = restarted.stage_states.get(task.id, "planning")
    assert stage is not None
    assert stage.artifact_refs is not None
    raw = stage.artifact_refs[REPAIR_SUBMISSION_ARTIFACT_KEY]
    decoded = decode_repair_submission(raw, expected_round_number=2)
    assert decoded.to_dict() == submission.to_dict()
    assert decoded.consumed_evidence_id is None

    consumed = restarted.stage_states.consume_plan_review_submission(
        task.id,
        "planning",
        raw_submission=raw,
        evidence_id="evidence-2",
    )
    replayed = restarted.stage_states.consume_plan_review_submission(
        task.id,
        "planning",
        raw_submission=raw,
        evidence_id="evidence-2",
    )

    assert consumed.artifact_refs == replayed.artifact_refs
    assert replayed.artifact_refs is not None
    receipt = decode_repair_submission(
        replayed.artifact_refs[REPAIR_SUBMISSION_ARTIFACT_KEY],
        expected_round_number=2,
    )
    assert receipt.to_dict() == submission.to_dict()
    assert receipt.consumed_evidence_id == "evidence-2"
    planner = Path("src/gobby/install/shared/workflows/agents/planner.yaml").read_text(
        encoding="utf-8"
    )
    assert "repair_submission" in planner


def test_sweep_universe_subset_refused(
    repair_setup: tuple[PlanReviewEvidenceService, str, Path],
) -> None:
    service, project_id, plan_path = repair_setup
    finding = _finding("finding-1")
    prior_evidence = _finalize_prior_round(
        service,
        project_id,
        plan_path,
        [finding],
    )
    _mark_plan_repaired(plan_path)
    current_snapshot = plan_path.read_bytes()
    universe = derive_repair_universe(
        prior_findings=[finding],
        inventory=_candidate_inventory("consumer-a", "consumer-b"),
    )
    attestation = _universe_attestation(finding, universe)
    attestation["consumer_sites_swept"] = ["consumer-a"]

    with pytest.raises(ReviewEvidenceError, match="consumer-b"):
        validate_repair_preparation(
            prior_evidence=prior_evidence,
            current_sections=build_section_manifest(current_snapshot),
            current_snapshot=current_snapshot,
            prior_finding_resolutions=[_resolution("finding-1", "repair")],
            repair_attestations=[attestation],
            repair_universe=universe,
        )


@pytest.mark.asyncio
async def test_universe_visible_before_attestation(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="repair-universe",
        repo_path=str(tmp_path),
    )
    finding = _finding("finding-1")
    universe = derive_repair_universe(
        prior_findings=[finding],
        inventory=_candidate_inventory("consumer-a"),
    )
    token = IndexToken(
        repository_digest="a" * 64,
        last_indexed_at="2026-07-27T00:00:00+00:00",
        source_files=("src/example.py",),
    )
    monkeypatch.setattr(
        review_evidence_tools,
        "_derive_settled_repair_universe",
        lambda **_kwargs: (token, universe),
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)

    result = await registry.call(
        "derive_plan_review_repair_universe",
        {
            "prior_evidence_id": "prior-evidence",
            "plan_path": str(tmp_path / ".gobby" / "plans" / "plan.md"),
            "repair_finding_ids": ["finding-1"],
        },
    )

    assert result["ok"] is True
    assert result["repair_universe"] == universe.to_dict()
    assert result["repair_universe_digest"] == universe.digest
    assert result["index_token"] == token.to_dict()
    skill = Path("src/gobby/install/shared/skills/plan/SKILL.md").read_text(encoding="utf-8")
    planner = Path("src/gobby/install/shared/workflows/agents/planner.yaml").read_text(
        encoding="utf-8"
    )
    for producer_contract in (skill, planner):
        normalized_contract = " ".join(producer_contract.split())
        assert "derive_plan_review_repair_universe" in normalized_contract
        assert "repair_universe_digest" in normalized_contract
        assert "Do not edit" in normalized_contract

    stale = _universe_attestation(finding, universe)
    stale["repair_universe_digest"] = "b" * 64
    with pytest.raises(ReviewEvidenceError, match="digest"):
        validate_repair_universe_attestations(
            universe=universe,
            attestations=[stale],
        )


def test_zero_result_requires_query_evidence() -> None:
    finding = _finding("finding-1")
    universe = derive_repair_universe(
        prior_findings=[finding],
        inventory=_candidate_inventory(),
    )
    attestation = _universe_attestation(finding, universe)
    attestation["sweep_query_evidence"] = []

    with pytest.raises(ReviewEvidenceError, match="query evidence"):
        validate_repair_universe_attestations(
            universe=universe,
            attestations=[attestation],
        )


def test_repair_bundle_interaction_edges() -> None:
    first = _finding("finding-1", check_key="repair.first")
    second = _finding("finding-2", check_key="repair.second")
    second["section_id"] = "2.1"
    universe = derive_repair_universe(
        prior_findings=[first, second],
        inventory=_candidate_inventory(
            "consumer-a",
            changed_targets=("src/shared.py",),
            changed_contracts=("contracts/shared.json",),
        ),
    )
    assert universe.interaction_edges
    assert universe.interaction_edges[0].shared_sections == ()
    assert universe.interaction_edges[0].shared_check_keys == ()
    first_attestation = _universe_attestation(first, universe)
    first_attestation["repair_bundle_interactions"] = []

    with pytest.raises(
        ReviewEvidenceError,
        match=universe.interaction_edges[0].edge_id,
    ):
        validate_repair_universe_attestations(
            universe=universe,
            attestations=[
                first_attestation,
                _universe_attestation(second, universe),
            ],
        )
