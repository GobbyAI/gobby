from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Never

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import (
    _validate_round_entry_plan,
    append_round_entry,
    atomic_write_bytes,
    build_section_manifest,
    ensure_checkpoint,
    manifest_key,
    parse_checkpoints,
    render_checkpoint,
)
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.plans.review_evidence_helpers import (
    ROUND_PROSE,
    bind_interactive_review,
    needs_review_result,
    repair_reviewed_section,
)
from tests.review_coverage_helpers import coverage_attestation


def test_prepare_round_snapshot(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    expected_snapshot = plan_path.read_bytes()

    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    assert (
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=1,
            session_id=session_id,
        ).evidence_id
        == prepared.evidence_id
    )
    with pytest.raises(ReviewEvidenceError) as active_attempt:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=session_id,
        )
    assert active_attempt.value.code == "review_round_active"

    assert prepared.plan_hash
    assert prepared.sections[0].section_id == "__preamble__"
    assert service.snapshot_bytes(prepared.evidence_id) == expected_snapshot
    row = service.get_evidence(prepared.evidence_id)
    assert row.snapshot == expected_snapshot
    assert row.session_id == session_id
    assert row.task_id is None
    assert row.lease_expires_at is not None


def test_prepare_finalized_interactive_round_returns_canonical_evidence(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    round_result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        ),
    }
    plan_path.write_bytes(
        plan_path.read_bytes()
        + b"\n"
        + service.render_plan_changelog_round(prepared.evidence_id, round_result)
    )
    service.finalize_plan_review_evidence(prepared.evidence_id, round_result)

    replay = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )

    assert replay.evidence_id == prepared.evidence_id


def test_prepare_next_round_after_finalized_checkpoint_from_other_session(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    round_result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        ),
    }
    ensure_checkpoint(
        plan_path,
        service.render_plan_changelog_round(prepared.evidence_id, round_result),
    )
    service.finalize_plan_review_evidence(prepared.evidence_id, round_result)

    successor = SessionManager(service.db).register(
        external_id="review-evidence-successor",
        machine_id=None,
        source="claude",
        project_id=project_id,
    )
    next_round = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=3,
        session_id=successor.id,
    )

    assert next_round.evidence_id != prepared.evidence_id
    assert service.get_evidence(next_round.evidence_id).session_id == successor.id


def _finalize_round_two(
    service: PlanReviewEvidenceService,
    project_id: str,
    session_id: str,
    plan_path: Path,
) -> tuple[str, str]:
    """Finalize a round-2 fence under ``session_id``; return (evidence_id, plan_hash)."""
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    round_result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        ),
    }
    ensure_checkpoint(
        plan_path,
        service.render_plan_changelog_round(prepared.evidence_id, round_result),
    )
    service.finalize_plan_review_evidence(prepared.evidence_id, round_result)
    return prepared.evidence_id, prepared.plan_hash


def test_reconcile_tolerates_session_rekey_on_finalized_evidence(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    """Session-identity unification re-keys finalized rows; their fences keep the old id."""
    service, project_id, session_id, plan_path = review_setup
    evidence_id, _plan_hash = _finalize_round_two(service, project_id, session_id, plan_path)
    successor = SessionManager(service.db).register(
        external_id="review-evidence-unified-successor",
        machine_id=None,
        source="claude",
        project_id=project_id,
    )
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE plan_review_evidence SET session_id = %s WHERE evidence_id = %s",
            (successor.id, evidence_id),
        )
    assert service.get_evidence(evidence_id).session_id == successor.id
    assert session_id.encode("utf-8") in plan_path.read_bytes()

    next_round = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=3,
        session_id=successor.id,
    )

    assert next_round.evidence_id != evidence_id
    assert service.get_evidence(evidence_id).finalized_at is not None


def test_reconcile_still_rejects_hash_drift_on_finalized_evidence(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    evidence_id, plan_hash = _finalize_round_two(service, project_id, session_id, plan_path)
    drifted = plan_path.read_bytes().replace(
        plan_hash.encode("utf-8"), ("f" * len(plan_hash)).encode("utf-8")
    )
    assert drifted != plan_path.read_bytes()
    plan_path.write_bytes(drifted)

    with pytest.raises(ReviewEvidenceError, match="lineage mismatch") as drift:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=3,
            session_id=session_id,
        )
    assert drift.value.code == "checkpoint_reconciliation_error"
    assert evidence_id in plan_path.read_text(encoding="utf-8")


def test_reconcile_rejects_checkpoint_session_forgery(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    forged = render_checkpoint(
        evidence_id=prepared.evidence_id,
        round_number=2,
        plan_hash=prepared.plan_hash,
        session_id="00000000-0000-4000-8000-00000000dead",
        round_result={
            "verdict": "needs_review",
            "findings": [],
            "coverage_attestation": coverage_attestation(
                evidence_id=prepared.evidence_id,
                shadow_valid=False,
            ),
        },
    )
    ensure_checkpoint(plan_path, forged)

    with pytest.raises(ReviewEvidenceError, match="lineage mismatch") as forgery:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=session_id,
        )
    assert forgery.value.code == "checkpoint_reconciliation_error"


def test_section_hash_canonicalization() -> None:
    snapshot = (
        b"# Title\n**Plan ID:** p\n\n"
        b"## 3.1 Work\nalpha\n\n"
        b"## Task   Mapping\nbeta\n\n"
        b"## V1 Plan Changelog\ngamma\n\n"
        b"## M1 Task Manifest\ndelta\n"
    )
    first = build_section_manifest(snapshot)
    second = build_section_manifest(snapshot)
    assert first == second
    assert [section.section_id for section in first] == [
        "__preamble__",
        "3.1",
        "Task Mapping",
        "V1",
        "M1",
    ]
    assert manifest_key("## 3.1 Work") == "3.1"
    assert manifest_key("## Task Mapping") == "Task Mapping"
    assert manifest_key("## V1 Plan Changelog") == "V1"
    assert manifest_key("## M1 Task Manifest") == "M1"

    changed = build_section_manifest(snapshot.replace(b"alpha", b"omega"))
    differing = [
        before.section_id
        for before, after in zip(first, changed, strict=True)
        if before.section_hash != after.section_hash
    ]
    assert differing == ["3.1"]

    for heading, key in [
        ("## 3.1 Again", "3.1"),
        ("## Task Mapping", "Task Mapping"),
        ("## V1 Another", "V1"),
        ("## M1 Another", "M1"),
    ]:
        duplicated = snapshot + f"\n{heading}\nrepeat\n".encode()
        with pytest.raises(ReviewEvidenceError, match=rf"duplicate manifest key: {re.escape(key)}"):
            build_section_manifest(duplicated)
    with pytest.raises(ReviewEvidenceError, match="duplicate manifest key: Ordinary"):
        build_section_manifest(snapshot + b"\n## Ordinary\none\n## Ordinary\ntwo\n")


def test_toctou_snapshot_isolation(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    original = plan_path.read_bytes()
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    plan_path.write_bytes(original.replace(b"Behavior exists.", b"Behavior changed."))

    assert service.snapshot_bytes(prepared.evidence_id) == original
    assert service.snapshot_payload(prepared.evidence_id)["snapshot"] == original


def test_stale_write_guard_and_lifecycle(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    original = plan_path.read_bytes()
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    plan_path.write_bytes(original.replace(b"Pending.", b"Coordinator update."))
    assert service.verify_plan_unchanged(prepared.evidence_id, plan_path)

    plan_path.write_bytes(plan_path.read_bytes().replace(b"# Review Evidence", b"# Changed Title"))
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        service.verify_plan_unchanged(prepared.evidence_id, plan_path)

    plan_path.write_bytes(original)
    with pytest.raises(ReviewEvidenceError) as replayed_coverage:
        service.render_plan_changelog_round(
            prepared.evidence_id,
            {
                "verdict": "needs_review",
                "findings": [],
                "coverage_attestation": coverage_attestation(
                    evidence_id="another-evidence",
                    shadow_valid=False,
                ),
            },
        )
    assert replayed_coverage.value.code == "coverage_evidence_mismatch"
    rejection = {
        "verdict": "needs_review",
        "findings": [{"message": "fix it"}],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        ),
    }
    checkpoint = service.render_plan_changelog_round(prepared.evidence_id, rejection)
    ensure_checkpoint(plan_path, checkpoint)
    next_round = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    recovered = service.get_evidence(prepared.evidence_id)
    assert recovered.finalized_at is not None
    assert recovered.expired_at is None
    assert recovered.round_result == rejection
    assert next_round.evidence_id != prepared.evidence_id

    dead_path = plan_path.with_name("dead-attempt.md")
    dead_path.write_bytes(original)
    dead = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=dead_path,
        round_number=1,
        session_id=session_id,
    )
    manager = LocalAgentRunManager(service.db)
    dead_run = manager.create(
        parent_session_id=session_id,
        provider="codex",
        prompt="dead review",
    )
    service.bind_evidence_run(dead.evidence_id, dead_run.id)
    manager.cancel(dead_run.id)
    replacement = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=dead_path,
        round_number=2,
        session_id=session_id,
    )
    assert service.get_evidence(dead.evidence_id).expired_at is not None
    assert replacement.evidence_id != dead.evidence_id

    live_path = plan_path.with_name("live-attempt.md")
    live_path.write_bytes(original)
    live = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=live_path,
        round_number=1,
        session_id=session_id,
    )
    live_run = manager.create(
        parent_session_id=session_id,
        provider="codex",
        prompt="live review",
    )
    service.bind_evidence_run(live.evidence_id, live_run.id)
    with pytest.raises(ReviewEvidenceError) as still_live:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=live_path,
            round_number=2,
            session_id=session_id,
        )
    assert still_live.value.code == "review_round_active"
    assert service.get_evidence(live.evidence_id).expired_at is None


def test_path_boundary_and_binding_validation(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    tmp_path: Path,
) -> None:
    service, project_id, session_id, plan_path = review_setup
    outside = tmp_path.parent / "outside-review.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    with pytest.raises(ReviewEvidenceError, match="escapes project root"):
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=outside,
            round_number=1,
            session_id=session_id,
        )
    symlink = plan_path.with_name("linked.md")
    symlink.symlink_to(plan_path)
    with pytest.raises(ReviewEvidenceError, match="symlinked plan paths"):
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=symlink,
            round_number=1,
            session_id=session_id,
        )

    task = LocalTaskManager(service.db).create_task(
        project_id,
        "Review stage evidence",
        validation_criteria="Review evidence remains bound to the requested task and stage.",
    )
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        task_id=task.id,
        stage="review",
    )
    with pytest.raises(ReviewEvidenceError) as pending:
        service.authorize_current_attempt(
            prepared.evidence_id,
            project_id=project_id,
            plan_path=plan_path,
            round_number=1,
            task_id=task.id,
            stage="review",
        )
    assert pending.value.code == "binding_pending"
    assert pending.value.retryable

    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
        task_id=task.id,
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    current = service.authorize_current_attempt(
        prepared.evidence_id,
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        task_id=task.id,
        stage="review",
        run_id=run.id,
    )
    assert current.evidence_id == prepared.evidence_id
    finalized = service.finalize_plan_review_evidence(
        prepared.evidence_id,
        {
            "verdict": "needs_review",
            "findings": [],
            "coverage_attestation": coverage_attestation(
                evidence_id=prepared.evidence_id,
                shadow_valid=False,
            ),
        },
    )
    replay = service.authorize_current_attempt(
        prepared.evidence_id,
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        task_id=task.id,
        stage="review",
        run_id=run.id,
        allow_rejection_replay=True,
    )
    assert replay.round_result == finalized.round_result
    assert (
        service.resolve_historical_proof(
            prepared.evidence_id,
            project_id=project_id,
            plan_path=plan_path,
            task_id=task.id,
        ).evidence_id
        == prepared.evidence_id
    )


def test_manifest_compare_and_apply(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)

    def canonical_approval(evidence_id: str) -> dict[str, object]:
        derived = service.derive_plan_review_manifest(
            evidence_id,
            routing_decisions={},
        )
        manifest_entries = derived["manifest_entries"]
        assert isinstance(manifest_entries, list)
        return {
            "verdict": "approved",
            "findings": [],
            "routing_decisions": {},
            "manifest_entries": manifest_entries,
            "coverage_attestation": coverage_attestation(
                evidence_id=evidence_id,
                manifest_entries=manifest_entries,
            ),
        }

    approval = canonical_approval(prepared.evidence_id)
    original_bytes = plan_path.read_bytes()
    invalid_shadow = service.derive_plan_review_manifest(
        prepared.evidence_id,
        routing_decisions={"missing": {}},
    )
    assert invalid_shadow["status"] == "invalid"
    assert plan_path.read_bytes() == original_bytes
    assert service.get_evidence(prepared.evidence_id).manifest_state is None

    approval_entries = approval["manifest_entries"]
    assert isinstance(approval_entries, list)
    tampered_entries = [dict(entry) for entry in approval_entries if isinstance(entry, dict)]
    tampered_entries[0]["title"] = "Caller-controlled drift"
    tampered = {**approval, "manifest_entries": tampered_entries}
    with pytest.raises(ReviewEvidenceError) as noncanonical:
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            tampered,
            plan_path=plan_path,
            run_id=run.id,
        )
    assert noncanonical.value.code == "noncanonical_manifest"
    assert plan_path.read_bytes() == original_bytes
    assert service.get_evidence(prepared.evidence_id).manifest_state is None

    def crash_atomic_write(_path: Path, _content: bytes) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        crash_atomic_write,
    )
    with pytest.raises(OSError, match="simulated crash"):
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            approval,
            plan_path=plan_path,
            run_id=run.id,
        )
    pending = service.get_evidence(prepared.evidence_id)
    assert pending.manifest_state is None
    assert pending.round_result is None
    assert plan_path.read_bytes() == original_bytes

    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )
    applied = service.apply_plan_review_manifest(
        prepared.evidence_id,
        approval,
        plan_path=plan_path,
        run_id=run.id,
    )
    row = service.get_evidence(prepared.evidence_id)
    assert row.manifest_state == "applied"
    assert row.round_result == approval
    assert row.finalized_at is None
    first_bytes = plan_path.read_bytes()
    assert (
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            approval,
            plan_path=plan_path,
            run_id=run.id,
        )
        == applied
    )
    assert plan_path.read_bytes() == first_bytes

    changed = {
        **approval,
        "findings": [
            {
                "finding_id": "DIFFERENT",
                "section_id": "1.1",
                "check_key": "format-drift",
                "severity": "nit",
                "category": "gobby-format",
                "location": "Section 1.1",
                "description": "The format differs from the recorded approval.",
                "fix": "Keep the approved format.",
                "prevention": "Compare the final format before applying the manifest.",
                "principle": "Persist one canonical approval payload.",
            }
        ],
    }
    with pytest.raises(ReviewEvidenceError, match="different manifest payload"):
        service.apply_plan_review_manifest(
            prepared.evidence_id,
            changed,
            plan_path=plan_path,
            run_id=run.id,
        )

    landed_path = plan_path.with_name("review-evidence-landed.md")
    landed_path.write_bytes(original_bytes)
    landed_prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=landed_path,
        round_number=1,
        session_id=session_id,
    )
    landed_run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review landed",
    )
    service.bind_evidence_run(landed_prepared.evidence_id, landed_run.id)
    landed_approval = canonical_approval(landed_prepared.evidence_id)
    complete_manifest_apply = service.store.complete_manifest_apply

    def crash_before_checkpoint(
        *,
        transaction: Transaction,
        evidence_id: str,
        result: Mapping[str, object],
    ) -> Never:
        _ = transaction, evidence_id, result
        raise RuntimeError("simulated checkpoint crash")

    monkeypatch.setattr(service.store, "complete_manifest_apply", crash_before_checkpoint)
    with pytest.raises(RuntimeError, match="simulated checkpoint crash"):
        service.apply_plan_review_manifest(
            landed_prepared.evidence_id,
            landed_approval,
            plan_path=landed_path,
            run_id=landed_run.id,
        )
    landed_bytes = landed_path.read_bytes()
    assert landed_bytes != original_bytes
    assert service.get_evidence(landed_prepared.evidence_id).manifest_state is None
    monkeypatch.setattr(
        service.store,
        "complete_manifest_apply",
        complete_manifest_apply,
    )
    service.apply_plan_review_manifest(
        landed_prepared.evidence_id,
        landed_approval,
        plan_path=landed_path,
        run_id=landed_run.id,
    )
    assert landed_path.read_bytes() == landed_bytes
    assert service.get_evidence(landed_prepared.evidence_id).manifest_state == "applied"

    drift_path = plan_path.with_name("review-evidence-drift.md")
    drift_path.write_bytes(original_bytes)
    drift_prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=drift_path,
        round_number=1,
        session_id=session_id,
    )
    drift_run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review drift",
    )
    service.bind_evidence_run(drift_prepared.evidence_id, drift_run.id)
    drift_approval = canonical_approval(drift_prepared.evidence_id)
    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        crash_atomic_write,
    )
    with pytest.raises(OSError, match="simulated crash"):
        service.apply_plan_review_manifest(
            drift_prepared.evidence_id,
            drift_approval,
            plan_path=drift_path,
            run_id=drift_run.id,
        )
    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.atomic_write_bytes",
        atomic_write_bytes,
    )
    drift_path.write_bytes(
        drift_path.read_bytes().replace(b"Behavior exists.", b"Behavior drifted.")
    )
    drifted_bytes = drift_path.read_bytes()
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        service.apply_plan_review_manifest(
            drift_prepared.evidence_id,
            drift_approval,
            plan_path=drift_path,
            run_id=drift_run.id,
        )
    revoked = service.get_evidence(drift_prepared.evidence_id)
    assert revoked.manifest_state is None
    assert revoked.round_result is None
    assert revoked.manifest_payload is None
    assert drift_path.read_bytes() == drifted_bytes
    with pytest.raises(ReviewEvidenceError, match="reviewed plan sections changed"):
        service.apply_plan_review_manifest(
            drift_prepared.evidence_id,
            drift_approval,
            plan_path=drift_path,
            run_id=drift_run.id,
        )
    LocalAgentRunManager(service.db).cancel(drift_run.id)
    rereview = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=drift_path,
        round_number=2,
        session_id=session_id,
    )
    assert rereview.evidence_id != drift_prepared.evidence_id
    assert service.get_evidence(drift_prepared.evidence_id).expired_at is not None


def test_two_phase_run_binding(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    manager = LocalAgentRunManager(service.db)
    run = manager.create(parent_session_id=session_id, provider="codex", prompt="review")
    bound = service.bind_evidence_run(prepared.evidence_id, run.id)
    assert bound.dispatch_run_id == run.id
    assert bound.lease_expires_at is None
    assert service.bind_evidence_run(prepared.evidence_id, run.id).dispatch_run_id == run.id

    other = manager.create(parent_session_id=session_id, provider="codex", prompt="other")
    with pytest.raises(ReviewEvidenceError, match="already bound"):
        service.bind_evidence_run(prepared.evidence_id, other.id)
    manager.cancel(run.id)
    expired = service.expire_plan_review_evidence(prepared.evidence_id)
    assert expired.expired_at is not None

    spawn_failed = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    assert (
        service.expire_plan_review_evidence(
            spawn_failed.evidence_id,
            spawn_failed=True,
        ).expired_at
        is not None
    )

    bind_failed = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=3,
        session_id=session_id,
    )
    other_session = SessionManager(service.db).register(
        external_id="other-review-parent",
        machine_id=None,
        source="codex",
        project_id=project_id,
    )
    wrong_run = manager.create(
        parent_session_id=other_session.id,
        provider="codex",
        prompt="wrong lineage",
    )
    with pytest.raises(ReviewEvidenceError, match="does not belong"):
        service.bind_evidence_run(bind_failed.evidence_id, wrong_run.id)
    assert service.get_evidence(bind_failed.evidence_id).expired_at is not None
    cancelled = manager.get(wrong_run.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_interactive_mint_status_lifecycle(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    derived = service.derive_plan_review_manifest(
        prepared.evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    approval = {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            manifest_entries=manifest_entries,
        ),
    }
    with pytest.raises(ReviewEvidenceError, match="V1 checkpoint"):
        service.finalize_plan_review_evidence(prepared.evidence_id, approval)
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="approve",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    service.apply_plan_review_manifest(
        prepared.evidence_id,
        approval,
        plan_path=plan_path,
        run_id=run.id,
    )
    checkpoint = service.render_plan_changelog_round(prepared.evidence_id)

    with pytest.raises(ReviewEvidenceError) as pending:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=session_id,
        )
    assert pending.value.code == "pending_lesson_mint"
    pending_rows = pending.value.details["pending"]
    assert isinstance(pending_rows, list)
    assert pending_rows
    pending_row = pending_rows[0]
    assert isinstance(pending_row, dict)
    assert pending_row["round_result"] == approval
    finalized = service.get_evidence(prepared.evidence_id)
    assert finalized.lesson_mint_status == "pending"
    assert checkpoint in plan_path.read_bytes()

    minted = service.checkpoint_plan_review_lesson_mint(
        prepared.evidence_id,
        status="minted",
        detail={"lesson_ids": ["lesson-1"]},
    )
    assert minted.lesson_mint_status == "minted"
    assert (
        service.checkpoint_plan_review_lesson_mint(
            prepared.evidence_id,
            status="minted",
            detail={"lesson_ids": ["lesson-1"]},
        ).lesson_mint_status
        == "minted"
    )
    assert service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )


def _round_entry_plan(tmp_path: Path) -> Path:
    plan_path = tmp_path / "round-entry.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Round Entry",
                "**Plan ID:** round-entry",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 Work",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Behavior exists. test: `tests/test_example.py`",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds yet.",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return plan_path


def _round_checkpoint(evidence_id: str = "evidence-1", round_number: int = 2) -> bytes:
    return render_checkpoint(
        evidence_id=evidence_id,
        round_number=round_number,
        plan_hash="hash",
        session_id="session",
        round_result={
            "verdict": "needs_review",
            "findings": [],
            "coverage_attestation": coverage_attestation(
                evidence_id=evidence_id,
                shadow_valid=False,
            ),
        },
    )


def test_append_round_entry_allows_multibyte_text_before_v1(tmp_path: Path) -> None:
    plan_path = _round_entry_plan(tmp_path)
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(
            "# Round Entry",
            "# Round Entry 日本語 — café 🎯",
            1,
        ),
        encoding="utf-8",
    )
    checkpoint = _round_checkpoint()

    assert append_round_entry(plan_path, ROUND_PROSE, checkpoint) is True
    text = plan_path.read_text(encoding="utf-8")
    assert "日本語 — café 🎯" in text
    assert text.index(ROUND_PROSE) < text.index(checkpoint.decode("utf-8"))


def test_append_round_entry_inserts_before_next_section(tmp_path: Path) -> None:
    plan_path = _round_entry_plan(tmp_path)
    checkpoint = _round_checkpoint()

    assert append_round_entry(plan_path, ROUND_PROSE, checkpoint) is True

    text = plan_path.read_text(encoding="utf-8")
    prose_index = text.index(ROUND_PROSE)
    fence_index = text.index(checkpoint.decode("utf-8"))
    assert text.index("No rounds yet.") < prose_index < fence_index < text.index("## Task Mapping")

    replayed = plan_path.read_bytes()
    assert append_round_entry(plan_path, ROUND_PROSE, checkpoint) is False
    assert plan_path.read_bytes() == replayed


def test_append_round_entry_allows_headings_inside_v1(tmp_path: Path) -> None:
    plan_path = _round_entry_plan(tmp_path)
    prose = "## Injected Section\n`kind: framing`\n\nprose"
    checkpoint = _round_checkpoint()

    assert append_round_entry(plan_path, prose, checkpoint) is True

    text = plan_path.read_text(encoding="utf-8")
    assert text.index("## V1 Plan Changelog") < text.index("## Injected Section")
    assert text.index("## Injected Section") < text.index("## Task Mapping")


def test_validate_round_entry_plan_rejects_changes_outside_v1(tmp_path: Path) -> None:
    plan_path = _round_entry_plan(tmp_path)
    current = plan_path.read_bytes()
    updated = current.replace(b"## Task Mapping", b"## Task Mapping Changed", 1)

    with pytest.raises(ReviewEvidenceError, match="only the V1"):
        _validate_round_entry_plan(plan_path, current, updated)


def test_append_round_entry_fails_atomically(tmp_path: Path) -> None:
    plan_path = _round_entry_plan(tmp_path)
    checkpoint = _round_checkpoint()
    original = plan_path.read_bytes()

    with pytest.raises(ReviewEvidenceError, match="prose cannot be empty"):
        append_round_entry(plan_path, "   ", checkpoint)
    assert plan_path.read_bytes() == original


def test_append_round_entry_rejects_conflicting_checkpoint(tmp_path: Path) -> None:
    plan_path = _round_entry_plan(tmp_path)
    assert append_round_entry(plan_path, ROUND_PROSE, _round_checkpoint()) is True
    snapshot = plan_path.read_bytes()

    with pytest.raises(ReviewEvidenceError, match="conflicting V1 checkpoint"):
        append_round_entry(
            plan_path,
            ROUND_PROSE,
            _round_checkpoint(round_number=3),
        )
    assert plan_path.read_bytes() == snapshot


def test_append_round_entry_rejects_checkpoint_fence_in_prose(tmp_path: Path) -> None:
    plan_path = _round_entry_plan(tmp_path)
    checkpoint = _round_checkpoint()
    original = plan_path.read_bytes()
    smuggled = _round_checkpoint(evidence_id="evidence-smuggled", round_number=9)
    prose = f"{ROUND_PROSE}\n\n{smuggled.decode('utf-8')}"

    with pytest.raises(ReviewEvidenceError, match="checkpoint list"):
        append_round_entry(plan_path, prose, checkpoint)
    assert plan_path.read_bytes() == original


def test_append_plan_changelog_round_needs_review_end_to_end(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    round_result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=prepared.evidence_id,
            shadow_valid=False,
        ),
    }

    result = service.append_plan_changelog_round(
        prepared.evidence_id,
        ROUND_PROSE,
        round_result,
    )

    assert result["applied"] is True
    rendered = service.render_plan_changelog_round(prepared.evidence_id, round_result)
    assert result["checkpoint"] == rendered.decode("utf-8")
    text = plan_path.read_text(encoding="utf-8")
    prose_index = text.index(ROUND_PROSE)
    fence_index = text.index(rendered.decode("utf-8"))
    assert text.index("## V1 Plan Changelog") < prose_index < fence_index
    assert fence_index < text.index("## M1 Task Manifest")

    service.finalize_plan_review_evidence(prepared.evidence_id, round_result)
    assert service.get_evidence(prepared.evidence_id).finalized_at is not None

    replay = service.append_plan_changelog_round(
        prepared.evidence_id,
        ROUND_PROSE,
        round_result,
    )
    assert replay["applied"] is False
    assert plan_path.read_text(encoding="utf-8") == text


def test_append_plan_changelog_round_writes_normalized_payload(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    evidence_id = bind_interactive_review(service, project_id, session_id, plan_path)
    round_result = needs_review_result(evidence_id)

    result = service.append_plan_changelog_round(
        evidence_id,
        ROUND_PROSE,
        round_result,
    )

    assert result["applied"] is True
    normalized = service._round_result_for_evidence(evidence_id, round_result)
    durable = parse_checkpoints(plan_path.read_bytes())[-1]
    assert durable["round_result"] == normalized
    rendered = service.render_plan_changelog_round(evidence_id, normalized)
    assert result["checkpoint"] == rendered.decode("utf-8")


def test_append_plan_changelog_round_after_needs_review_repairs(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    evidence_id = bind_interactive_review(service, project_id, session_id, plan_path)
    repair_reviewed_section(plan_path)
    with pytest.raises(ReviewEvidenceError) as stale:
        service.verify_plan_unchanged(evidence_id, plan_path)
    assert stale.value.code == "stale_plan_evidence"

    round_result = needs_review_result(evidence_id)
    result = service.append_plan_changelog_round(evidence_id, ROUND_PROSE, round_result)

    assert result["applied"] is True
    rendered = service.render_plan_changelog_round(evidence_id, round_result)
    text = plan_path.read_text(encoding="utf-8")
    assert "Repaired behavior exists." in text
    assert text.index(ROUND_PROSE) < text.index(rendered.decode("utf-8"))
    service.finalize_plan_review_evidence(evidence_id, round_result)
    assert service.get_evidence(evidence_id).finalized_at is not None


def test_append_plan_changelog_round_rejects_unbound_evidence(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )

    with pytest.raises(ReviewEvidenceError) as unbound:
        service.append_plan_changelog_round(
            prepared.evidence_id,
            ROUND_PROSE,
            needs_review_result(prepared.evidence_id),
        )
    assert unbound.value.code == "binding_pending"


def test_append_plan_changelog_round_rejects_expired_evidence(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    manager = LocalAgentRunManager(service.db)
    run = manager.create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    manager.cancel(run.id)
    service.expire_plan_review_evidence(prepared.evidence_id)

    with pytest.raises(ReviewEvidenceError) as expired:
        service.append_plan_changelog_round(
            prepared.evidence_id,
            ROUND_PROSE,
            needs_review_result(prepared.evidence_id),
        )
    assert expired.value.code == "evidence_replay"


def test_append_plan_changelog_round_rejects_wrong_plan_path(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    evidence_id = bind_interactive_review(service, project_id, session_id, plan_path)
    other = plan_path.with_name("other-plan.md")
    other.write_bytes(plan_path.read_bytes())

    with pytest.raises(ReviewEvidenceError) as wrong:
        service.append_plan_changelog_round(
            evidence_id,
            ROUND_PROSE,
            needs_review_result(evidence_id),
            plan_path=other,
        )
    assert wrong.value.code == "wrong_plan"


def test_append_plan_changelog_round_approved_still_requires_identity(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, session_id, plan_path = review_setup
    evidence_id = bind_interactive_review(service, project_id, session_id, plan_path)
    repair_reviewed_section(plan_path)
    derived = service.derive_plan_review_manifest(evidence_id, routing_decisions={})
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    approved = {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=manifest_entries,
        ),
    }

    with pytest.raises(ReviewEvidenceError) as stale:
        service.append_plan_changelog_round(evidence_id, ROUND_PROSE, approved)
    assert stale.value.code == "stale_plan_evidence"


def test_review_evidence_resolve_plan_path_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
    from gobby.plans.review_manifest_service import ReviewManifestService
    from gobby.storage.projects import LocalProjectManager
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    plan_dir = tmp_path / "repo" / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "review-evidence.md"
    plan_path.write_text("# Review\n", encoding="utf-8")
    service = ReviewManifestService(
        db=temp_db,
        store=PlanReviewEvidenceStore(temp_db),
        projects=LocalProjectManager(temp_db),
    )

    resolved, relative = service.resolve_plan_path(isolated.project.id, plan_path)

    assert resolved == plan_path.resolve()
    assert relative == ".gobby/plans/review-evidence.md"


def test_review_evidence_resolve_plan_path_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
    from gobby.plans.review_manifest_service import ReviewManifestService
    from gobby.storage.projects import LocalProjectManager
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="review-no-checkout")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n", encoding="utf-8")
    service = ReviewManifestService(
        db=temp_db,
        store=PlanReviewEvidenceStore(temp_db),
        projects=LocalProjectManager(temp_db),
    )

    with pytest.raises(ReviewEvidenceError):
        service.resolve_plan_path(project.id, plan_path)
