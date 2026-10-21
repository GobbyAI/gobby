from __future__ import annotations

import hashlib
import json
import re

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.tasks._artifacts import TaskArtifactManager
from tests.review_coverage_helpers import coverage_attestation
from tests.storage.stage_review_helpers import (
    StageReviewSetup,
    _prepare_bound,
)
from tests.storage.stage_review_helpers import (
    stage_review_setup as _stage_review_setup,  # noqa: F401 - pytest discovers imported fixtures
)
from tests.storage.tasks._stage_test_helpers import (
    lifecycle_events,
    set_stage_state,
    stage_row,
)


def _findings() -> list[dict[str, object]]:
    return [
        {
            "finding_id": "F1",
            "section_id": "1.1",
            "check_key": "failure-atomicity",
            "severity": "blocking",
            "category": "unhandled-edge",
            "location": "§ 1.1",
            "description": "The failure path can leave partial state.",
            "fix": "Specify rollback before retry.",
            "root_cause": "Only the successful write was modeled.",
            "prevention": "Walk every write failure boundary.",
            "participating_section_ids": ["1.1"],
        },
        {
            "finding_id": "F2",
            "section_id": "1.1",
            "check_key": "cross-round-causality",
            "severity": "blocking",
            "category": "bad-sequencing",
            "location": "§ 1.1",
            "description": "The prior fix created a conflicting order.",
            "fix": "Restore the prerequisite before the new write.",
            "principle": "Causal fixes preserve established prerequisites.",
            "prevention": "Recheck all sections changed by a causal fix.",
            "introduced_in_round": 1,
            "causal_finding_id": "F1",
            "causal_section_ids": ["1.1"],
        },
    ]


def _fence(description: str) -> dict[str, object]:
    matches = re.findall(r"```json\n(.+?)\n```", description, flags=re.DOTALL)
    assert len(matches) == 1
    payload = json.loads(matches[0])
    assert isinstance(payload, dict)
    return payload


def test_fence_round_trip(stage_review_setup: StageReviewSetup) -> None:
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    findings = _findings()
    invalid = [dict(finding) for finding in findings]
    invalid[0]["section_id"] = "missing"
    with pytest.raises(ReviewEvidenceError, match="absent from the evidence manifest"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=invalid,
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id=run_id,
        )
    assert stage_review_setup.evidence.get_evidence(evidence_id).finalized_at is None

    updated = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=findings,
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )

    payload = _fence(updated.description or "")
    assert payload["findings"] == findings
    assert payload["evidence_id"] == evidence_id
    assert "principle" not in findings[0]
    assert findings[1]["causal_finding_id"] == "F1"
    evidence = stage_review_setup.evidence.get_evidence(evidence_id)
    assert evidence.round_result == {
        "verdict": "needs_review",
        "findings": findings,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
    }


def test_server_side_evidence_resolution(stage_review_setup: StageReviewSetup) -> None:
    original_snapshot = stage_review_setup.plan_path.read_bytes()
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    stored = stage_review_setup.evidence.get_evidence(evidence_id)
    stage_review_setup.plan_path.write_text("# Mutated live plan\n", encoding="utf-8")

    updated = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=_findings(),
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )

    payload = _fence(updated.description or "")
    assert payload["plan_hash"] == hashlib.sha256(original_snapshot).hexdigest()
    assert payload["plan_hash"] == stored.plan_hash
    assert (
        payload["plan_hash"]
        != hashlib.sha256(stage_review_setup.plan_path.read_bytes()).hexdigest()
    )
    assert payload["section_manifest"] == [section.to_dict() for section in stored.section_manifest]
    with pytest.raises(ReviewEvidenceError, match="current review attempt"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=2,
            dispatch_run_id=run_id,
        )

    other_plan = stage_review_setup.plan_path.with_name("other.md")
    other_plan.write_text(original_snapshot.decode("utf-8"), encoding="utf-8")
    other_task = stage_review_setup.manager.create_task(
        stage_review_setup.project_id,
        "Other plan",
        task_type="task",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    stage_review_setup.manager.initialize_task_manifest(other_task.id, stage_names=["planning"])
    set_stage_state(stage_review_setup.db, other_task.id, "planning", "needs_review")
    TaskArtifactManager(stage_review_setup.db).set_artifacts_atomic(
        other_task.id,
        plan_file_path=str(other_plan),
    )
    with pytest.raises(ReviewEvidenceError, match="current review attempt"):
        stage_review_setup.manager.reject_review(
            other_task.id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id=run_id,
        )


def test_rejection_persists_repairs_without_editing_plan(
    stage_review_setup: StageReviewSetup,
) -> None:
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    plan_before = stage_review_setup.plan_path.read_bytes()
    repairs = [
        {"kind": "add_targets", "section_id": "1.1", "entries": ["`src/consumer.py`"]},
        {
            "kind": "add_acceptance",
            "section_id": "1.1",
            "items": [{"prose": "Consumer updated", "artifact": "file: `src/consumer.py`"}],
        },
    ]
    findings = _findings()[:1]
    findings[0]["category"] = "traceability"
    findings[0]["repairs"] = repairs

    updated = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=findings,
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )

    description = updated.description or ""
    assert "**Repairs:**" in description
    assert "- add_targets 1.1: `src/consumer.py`" in description
    assert "- add_acceptance 1.1: Consumer updated. file: `src/consumer.py`" in description
    fence_findings = _fence(description)["findings"]
    assert isinstance(fence_findings, list)
    assert fence_findings[0]["repairs"] == repairs
    evidence = stage_review_setup.evidence.get_evidence(evidence_id)
    assert evidence.round_result is not None
    stored_findings = evidence.round_result["findings"]
    assert isinstance(stored_findings, list)
    assert stored_findings[0]["repairs"] == repairs
    assert stage_review_setup.plan_path.read_bytes() == plan_before


def test_free_text_rejection_fallback(stage_review_setup: StageReviewSetup) -> None:
    updated = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        rejection_notes="The fallback remains available.",
        round_number=1,
    )

    assert (
        stage_row(stage_review_setup.db, stage_review_setup.task_id, "planning")["state"] == "ready"
    )
    assert "The fallback remains available." in (updated.description or "")
    assert "```json" not in (updated.description or "")


def test_rejection_finalizes_evidence(
    stage_review_setup: StageReviewSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_id, run_id = _prepare_bound(stage_review_setup)
    original_finalize = PlanReviewEvidenceService.finalize_plan_review_evidence

    def crash_finalize(
        self: PlanReviewEvidenceService,
        target_evidence_id: str,
        round_result: dict[str, object],
    ) -> None:
        del self, target_evidence_id, round_result
        raise RuntimeError("crash between rejection writes")

    monkeypatch.setattr(
        PlanReviewEvidenceService,
        "finalize_plan_review_evidence",
        crash_finalize,
    )
    with pytest.raises(RuntimeError, match="crash between rejection writes"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id=run_id,
        )

    after_crash = stage_review_setup.manager.get_task(stage_review_setup.task_id)
    assert "```json" not in (after_crash.description or "")
    assert (
        stage_row(stage_review_setup.db, stage_review_setup.task_id, "planning")["state"]
        == "needs_review"
    )
    assert stage_review_setup.evidence.get_evidence(evidence_id).finalized_at is None

    monkeypatch.setattr(
        PlanReviewEvidenceService,
        "finalize_plan_review_evidence",
        original_finalize,
    )
    first = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=_findings(),
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )
    finalized = stage_review_setup.evidence.get_evidence(evidence_id)
    assert finalized.finalized_at is not None
    assert (
        stage_row(stage_review_setup.db, stage_review_setup.task_id, "planning")["state"] == "ready"
    )

    event_count = len(lifecycle_events(stage_review_setup.db, stage_review_setup.task_id))
    replay = stage_review_setup.manager.reject_review(
        stage_review_setup.task_id,
        "planning",
        findings=_findings(),
        coverage_attestation=coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
        evidence_id=evidence_id,
        round_number=1,
        dispatch_run_id=run_id,
    )
    assert replay.description == first.description
    assert len(lifecycle_events(stage_review_setup.db, stage_review_setup.task_id)) == event_count
    assert (
        stage_review_setup.evidence.get_evidence(evidence_id).finalized_at == finalized.finalized_at
    )

    with pytest.raises(ReviewEvidenceError, match="no longer live"):
        stage_review_setup.manager.reject_review(
            stage_review_setup.task_id,
            "planning",
            findings=_findings(),
            coverage_attestation=coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
            evidence_id=evidence_id,
            round_number=1,
            dispatch_run_id="wrong-run",
        )

    next_round = stage_review_setup.evidence.prepare_plan_review_round(
        project_id=stage_review_setup.project_id,
        plan_path=stage_review_setup.plan_path,
        round_number=2,
        task_id=stage_review_setup.task_id,
        stage="planning",
    )
    assert next_round.evidence_id != evidence_id
    assert stage_review_setup.evidence.get_evidence(evidence_id).finalized_at is not None
