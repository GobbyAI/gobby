"""Contract tests for the bundled review-learning skill and producer hooks."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from gobby.code_index.models import IndexedProject, IndexWriteMode
from gobby.code_index.storage import CodeIndexStorage
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import atomic_write_bytes, ensure_checkpoint
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.plans.review_evidence_helpers import bind_review_seats
from tests.review_coverage_helpers import coverage_attestation
from tests.review_learning.conftest import FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _manifest_entries(stem: str) -> list[dict[str, object]]:
    return [
        {
            "title": f"Implement {name}",
            "source_section": section,
            "covers": [acceptance],
            "category": "code",
            "implementation_domain": "backend",
            "priority": 2,
            "task_type": "feature",
            "tdd": False,
            "labels": [f"covers:{stem}:{section}:{acceptance}"],
            "description": f"Implement {name}.",
            "validation_criteria": f"{name} behavior is tested.",
        }
        for name, section, acceptance in (
            ("A", "1.1", "1.1.1"),
            ("B", "1.2", "1.2.1"),
        )
    ]


def _manifest_yaml(stem: str) -> list[str]:
    lines: list[str] = []
    for entry in _manifest_entries(stem):
        covers = entry["covers"]
        labels = entry["labels"]
        assert isinstance(covers, list)
        assert isinstance(labels, list)
        lines.extend(
            [
                f"- title: {entry['title']}",
                f"  source_section: '{entry['source_section']}'",
                "  covers:",
                f"    - {covers[0]}",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels:",
                f"    - {labels[0]}",
                f"  description: {entry['description']}",
                f"  validation_criteria: {entry['validation_criteria']}",
            ]
        )
    return lines


def _review_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
    stem: str,
) -> tuple[PlanReviewEvidenceService, str, str, Path]:
    root = tmp_path / stem
    root.mkdir()
    project = install_isolated_checkout_project(
        temp_db, root, name=stem, machine_id=require_machine_id()
    ).project
    CodeIndexStorage(temp_db).upsert_project_stats(
        IndexedProject(id=project.id, root_path=str(root)), mode=IndexWriteMode.PRIMARY
    )
    session = SessionManager(temp_db).register(
        external_id=f"{stem}-parent",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=project.id,
    )
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "plan.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Review Learning",
                f"**Plan ID:** {stem}",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 A",
                "`kind: deliverable`",
                "",
                "Target: `src/a.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — A works. test: `tests/test_a.py::test_a`",
                "",
                "### 1.2 B",
                "`kind: deliverable`",
                "",
                "Target: `src/b.py`",
                "",
                "**Acceptance:**",
                "- 1.2.1 — B works. test: `tests/test_b.py::test_b`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds yet.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                *_manifest_yaml(stem),
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return PlanReviewEvidenceService(temp_db), project.id, session.id, plan_path


def _approval(
    service: PlanReviewEvidenceService,
    evidence_id: str,
) -> dict[str, object]:
    derived = service.derive_plan_review_manifest(evidence_id, routing_decisions={})
    entries = derived["manifest_entries"]
    assert isinstance(entries, list)
    return {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=entries,
        ),
    }


@pytest.mark.asyncio
async def test_plan_loop_recording_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.review_learning.service._current_project_id",
        lambda: "plan-loop-project",
    )
    memories = FakeMemoryManager()
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, memories),
        cast(RetirementTaskManager, FakeTaskManager()),
    )
    events: list[str] = []

    await service.record(
        source_kind="plan_review",
        source="seeded-plan-review",
        source_review="seed-round",
        decision="confirmed",
        finding={
            "title": "Cross-section invariants were missed",
            "lesson_type": "reviewer-miss",
            "pattern_id": "plan-review:reviewer-miss:unhandled-edge:cross-section-state",
            "check_key": "cross-section-state",
            "category": "unhandled-edge",
            "principle": "Review every section participating in one invariant",
            "prevention": "Check the complete participating section set",
            "guardrail_target": "checklist",
            "rule_id": "plan-review:unhandled-edge",
        },
        evidence={"participating_section_ids": ["1.1", "1.2"], "rounds_missed": 2},
    )
    recalled = await service.recall_review_lessons_by_class(
        "plan",
        ["reviewer-miss"],
    )
    events.append("recall")
    assert recalled["count"] == 1

    cases = [
        ("unproven", "weak-testability", []),
        ("cross-section", "bad-sequencing", ["1.1", "1.2"]),
    ]
    recorded_findings: list[str] = []
    for finding_id, category, participating in cases:
        if not participating:
            continue
        events.append("record")
        result = await service.record(
            source_kind="plan_review",
            source="interactive-approval",
            source_review="approval-round-3",
            decision="confirmed",
            finding={
                "title": f"{finding_id} reviewer miss",
                "lesson_type": "reviewer-miss",
                "pattern_id": f"plan-review:reviewer-miss:{category}:{finding_id}",
                "check_key": finding_id,
                "category": category,
                "principle": "Preserve evidence-backed plan review lessons",
                "prevention": "Review every participating section",
                "guardrail_target": "checklist",
                "rule_id": f"plan-review:{category}",
            },
            evidence={"participating_section_ids": participating},
        )
        recorded_findings.append(finding_id)
        assert result["lesson_id"]

    assert events[0] == "recall"
    assert recorded_findings == ["cross-section"]
    recorded = memories.memories[1:]
    assert len(recorded) == 1
    assert all("lesson-domain:plan" in (memory.tags or []) for memory in recorded)
    assert all("- guardrail_target: checklist" in memory.content for memory in recorded)
    assert all("- rule_id: plan-review:" in memory.content for memory in recorded)
    assert "participating_section_ids" in recorded[0].content


def test_interactive_approval_sequence(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, session_id, plan_path = _review_setup(
        temp_db,
        tmp_path,
        "approval-sequence",
    )
    rejected = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    bind_review_seats(service, rejected.evidence_id, session_id)
    rejection = {
        "verdict": "needs_review",
        "coverage_attestation": coverage_attestation(
            evidence_id=rejected.evidence_id,
            shadow_valid=False,
        ),
        "findings": [
            {
                "finding_id": "round-1-miss",
                "section_id": "1.1",
                "check_key": "cross-section-state",
                "severity": "blocking",
                "category": "unhandled-edge",
                "location": "Section 1.1",
                "description": "The shared state invariant is incomplete.",
                "fix": "Define the invariant across both participating sections.",
                "principle": "Review the whole invariant",
                "prevention": "Check both participating sections",
                "participating_section_ids": ["1.1", "1.2"],
            }
        ],
    }
    rejected_checkpoint = service.render_plan_changelog_round(
        rejected.evidence_id,
        rejection,
    )
    assert ensure_checkpoint(plan_path, rejected_checkpoint)
    service.finalize_plan_review_evidence(
        rejected.evidence_id, rejection, caller_session_id=session_id
    )

    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    _, coordinator_id = bind_review_seats(service, prepared.evidence_id, session_id)
    approval = _approval(service, prepared.evidence_id)
    applied = service.apply_plan_review_manifest(
        prepared.evidence_id,
        approval,
        plan_path=plan_path,
        caller_session_id=coordinator_id,
    )
    applied_bytes = plan_path.read_bytes()
    assert rejected_checkpoint in applied_bytes
    assert (
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            approval,
            plan_path=plan_path,
            caller_session_id=coordinator_id,
        )
        == applied
    )
    assert plan_path.read_bytes() == applied_bytes

    plan_path.write_bytes(applied_bytes.replace(b"A works.", b"A changed after apply."))
    approval_checkpoint = service.render_plan_changelog_round(prepared.evidence_id)
    assert ensure_checkpoint(plan_path, approval_checkpoint)
    finalized = service.finalize_plan_review_evidence(
        prepared.evidence_id, approval, caller_session_id=session_id
    )
    assert finalized.lesson_mint_status == "pending"
    proof = service.resolve_historical_proof(
        rejected.evidence_id,
        project_id=project_id,
        plan_path=plan_path,
        session_id=session_id,
    )
    assert proof.round_result == rejection
    minted = service.checkpoint_plan_review_lesson_mint(
        prepared.evidence_id,
        status="minted",
        detail={"lesson_ids": ["round-1-miss"]},
        caller_session_id=session_id,
    )
    assert minted.lesson_mint_status == "minted"
    assert service.checkpoint_plan_review_lesson_mint(
        prepared.evidence_id,
        status="minted",
        detail={"lesson_ids": ["round-1-miss"]},
        caller_session_id=session_id,
    ).lesson_mint_detail == {"lesson_ids": ["round-1-miss"]}

    stale_service, stale_project, stale_session, stale_path = _review_setup(
        temp_db,
        tmp_path,
        "pre-apply-drift",
    )
    stale = stale_service.prepare_plan_review_round(
        project_id=stale_project,
        plan_path=stale_path,
        round_number=1,
        session_id=stale_session,
    )
    _, stale_coordinator_id = bind_review_seats(stale_service, stale.evidence_id, stale_session)
    stale_path.write_bytes(stale_path.read_bytes().replace(b"A works.", b"A drifted."))
    drifted_bytes = stale_path.read_bytes()
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        stale_service.apply_plan_review_manifest(
            stale.evidence_id,
            _approval(stale_service, stale.evidence_id),
            plan_path=stale_path,
            caller_session_id=stale_coordinator_id,
        )
    stale_row = stale_service.get_evidence(stale.evidence_id)
    assert stale_row.manifest_state is None
    assert stale_row.round_result is None
    assert stale_path.read_bytes() == drifted_bytes

    pending_service, pending_project, pending_session, pending_path = _review_setup(
        temp_db,
        tmp_path,
        "pending-drift",
    )
    pending = pending_service.prepare_plan_review_round(
        project_id=pending_project,
        plan_path=pending_path,
        round_number=1,
        session_id=pending_session,
    )
    _, pending_coordinator_id = bind_review_seats(
        pending_service, pending.evidence_id, pending_session
    )

    def crash_write(_path: Path, _content: bytes) -> None:
        raise OSError("simulated apply crash")

    monkeypatch.setattr("gobby.plans.review_manifest_service.atomic_write_bytes", crash_write)
    with pytest.raises(OSError, match="simulated apply crash"):
        pending_service.apply_plan_review_manifest(
            pending.evidence_id,
            _approval(pending_service, pending.evidence_id),
            plan_path=pending_path,
            caller_session_id=pending_coordinator_id,
        )
    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )
    pending_path.write_bytes(
        pending_path.read_bytes().replace(b"A works.", b"A drifted while pending.")
    )
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        pending_service.apply_plan_review_manifest(
            pending.evidence_id,
            _approval(pending_service, pending.evidence_id),
            plan_path=pending_path,
            caller_session_id=pending_coordinator_id,
        )
    revoked = pending_service.get_evidence(pending.evidence_id)
    assert revoked.manifest_state is None
    assert revoked.round_result is None
    SessionManager(temp_db).update_status(pending_coordinator_id, "expired")
    pending_service.expire_plan_review_evidence(
        pending.evidence_id, caller_session_id=pending_session
    )
    replacement = pending_service.prepare_plan_review_round(
        project_id=pending_project,
        plan_path=pending_path,
        round_number=2,
        session_id=pending_session,
    )
    assert replacement.evidence_id != pending.evidence_id

    recovery_service, recovery_project, recovery_session, recovery_path = _review_setup(
        temp_db,
        tmp_path,
        "pending-recovery",
    )
    recovery = recovery_service.prepare_plan_review_round(
        project_id=recovery_project,
        plan_path=recovery_path,
        round_number=1,
        session_id=recovery_session,
    )
    _, recovery_coordinator_id = bind_review_seats(
        recovery_service, recovery.evidence_id, recovery_session
    )
    monkeypatch.setattr("gobby.plans.review_manifest_service.atomic_write_bytes", crash_write)
    with pytest.raises(OSError, match="simulated apply crash"):
        recovery_service.apply_plan_review_manifest(
            recovery.evidence_id,
            _approval(recovery_service, recovery.evidence_id),
            plan_path=recovery_path,
            caller_session_id=recovery_coordinator_id,
        )
    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )
    restarted = PlanReviewEvidenceService(temp_db)
    recovery_approval = _approval(restarted, recovery.evidence_id)
    restarted.apply_plan_review_manifest(
        recovery.evidence_id,
        recovery_approval,
        plan_path=recovery_path,
        caller_session_id=recovery_coordinator_id,
    )
    recovered_checkpoint = restarted.render_plan_changelog_round(recovery.evidence_id)
    assert ensure_checkpoint(recovery_path, recovered_checkpoint)
    restarted.finalize_plan_review_evidence(
        recovery.evidence_id, recovery_approval, caller_session_id=recovery_session
    )
    with pytest.raises(ReviewEvidenceError) as pending_mint:
        restarted.prepare_plan_review_round(
            project_id=recovery_project,
            plan_path=recovery_path,
            round_number=2,
            session_id=recovery_session,
        )
    assert pending_mint.value.code == "pending_lesson_mint"
    recovered_row = restarted.get_evidence(recovery.evidence_id)
    assert recovered_row.manifest_state == "applied"
    assert recovered_row.lesson_mint_status == "pending"
    assert recovery_path.read_bytes().count(recovered_checkpoint) == 1
    restarted.checkpoint_plan_review_lesson_mint(
        recovery.evidence_id,
        status="minted",
        detail={"lesson_ids": ["recovered"]},
        caller_session_id=recovery_session,
    )
    assert restarted.prepare_plan_review_round(
        project_id=recovery_project,
        plan_path=recovery_path,
        round_number=2,
        session_id=recovery_session,
    )
