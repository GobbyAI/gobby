"""Session-bound plan review without a spawned AgentRun."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory
from tests.plans.review_evidence_helpers import needs_review_result
from tests.review_coverage_helpers import coverage_attestation


def _seats(db: HubDatabase, project_id: str, reviewer_id: str) -> tuple[str, str]:
    sessions = SessionManager(db)
    reviewer = sessions.get(reviewer_id)
    assert reviewer is not None
    writer = sessions.register(
        external_id=f"static-writer-{uuid.uuid4()}",
        machine_id=reviewer.machine_id,
        source="codex",
        project_id=project_id,
    )
    coordinator = sessions.register(
        external_id=f"static-coordinator-{uuid.uuid4()}",
        machine_id=reviewer.machine_id,
        source="codex",
        project_id=project_id,
    )
    return writer.id, coordinator.id


def _prepare(
    service: PlanReviewEvidenceService, project_id: str, reviewer_id: str, plan_path: Path
) -> str:
    return service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=reviewer_id,
    ).evidence_id


def _bind(
    service: PlanReviewEvidenceService,
    evidence_id: str,
    reviewer_id: str,
    writer_id: str,
    coordinator_id: str,
) -> None:
    service.bind_static_review_seats(
        evidence_id,
        writer_session_id=writer_id,
        coordinator_session_id=coordinator_id,
        caller_session_id=reviewer_id,
    )


def test_static_bind_adopts_only_current_unchanged_evidence(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    original = plan_path.read_bytes()
    with service.db.transaction() as transaction:
        transaction.execute(
            "UPDATE plan_review_evidence SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE evidence_id = %s",
            (evidence_id,),
        )

    for caller in (None, writer_id):
        with pytest.raises(ReviewEvidenceError) as unauthorized:
            service.bind_static_review_seats(
                evidence_id,
                writer_session_id=writer_id,
                coordinator_session_id=coordinator_id,
                caller_session_id=caller,
            )
        assert unauthorized.value.code == "unauthorized_seat"
    with pytest.raises(ReviewEvidenceError) as missing:
        _bind(service, evidence_id, reviewer_id, str(uuid.uuid4()), coordinator_id)
    assert missing.value.code == "invalid_seats"

    plan_path.write_bytes(original.replace(b"Pending.", b"Changed."))
    with pytest.raises(ReviewEvidenceError) as stale:
        _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    assert stale.value.code == "stale_snapshot"
    plan_path.write_bytes(original)

    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    bound = service.get_evidence(evidence_id)
    assert bound.static_writer_session_id == writer_id
    assert bound.static_coordinator_session_id == coordinator_id
    assert bound.dispatch_run_id is None
    assert bound.lease_expires_at is None
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    with pytest.raises(ReviewEvidenceError) as still_live:
        service.expire_plan_review_evidence(
            evidence_id, spawn_failed=True, caller_session_id=reviewer_id
        )
    assert still_live.value.code == "attempt_still_live"
    with pytest.raises(ReviewEvidenceError) as changed:
        _bind(service, evidence_id, reviewer_id, coordinator_id, writer_id)
    assert changed.value.code == "evidence_already_bound"

    run = LocalAgentRunManager(service.db).create(
        parent_session_id=reviewer_id, provider="codex", prompt="competing run"
    )
    with pytest.raises(ReviewEvidenceError) as competing:
        service.bind_evidence_run(evidence_id, run.id)
    assert competing.value.code == "evidence_already_bound"
    assert service.get_evidence(evidence_id).expired_at is None


def test_static_bind_rejects_ended_seat(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    SessionManager(service.db).update_status(writer_id, "expired")

    with pytest.raises(ReviewEvidenceError) as ended:
        _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    assert ended.value.code == "invalid_seats"
    assert service.get_evidence(evidence_id).static_writer_session_id is None


def test_static_abandoned_seat_recovery_protects_live_seats(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    outsider_id, _ = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)

    with pytest.raises(ReviewEvidenceError) as live:
        service.expire_plan_review_evidence(evidence_id, caller_session_id=reviewer_id)
    assert live.value.code == "attempt_still_live"

    SessionManager(service.db).update_status(writer_id, "expired")
    with pytest.raises(ReviewEvidenceError) as outsider:
        service.expire_plan_review_evidence(evidence_id, caller_session_id=outsider_id)
    assert outsider.value.code == "unauthorized_seat"
    with pytest.raises(ReviewEvidenceError) as invalid_caller:
        service.expire_plan_review_evidence(evidence_id, caller_session_id="not-a-session")
    assert invalid_caller.value.code == "unauthorized_seat"
    with pytest.raises(ReviewEvidenceError) as ended_caller:
        service.expire_plan_review_evidence(evidence_id, caller_session_id=writer_id)
    assert ended_caller.value.code == "unauthorized_seat"

    expired = service.expire_plan_review_evidence(evidence_id, caller_session_id=reviewer_id)
    assert expired.expired_at is not None
    successor = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=reviewer_id,
    )
    assert successor.evidence_id != evidence_id


def test_static_abandoned_reviewer_after_checkpoint_allows_successor_round(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    result = needs_review_result(evidence_id)
    service.append_plan_changelog_round(
        evidence_id, "**Round 1**", result, caller_session_id=writer_id
    )
    SessionManager(service.db).update_status(reviewer_id, "expired")

    expired = service.expire_plan_review_evidence(evidence_id, caller_session_id=coordinator_id)
    assert expired.expired_at is not None
    assert expired.round_result is None
    successor = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=coordinator_id,
    )
    assert successor.evidence_id != evidence_id


def test_static_pending_manifest_can_be_retired_after_coordinator_ends(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    # Model a committed intent left by an interrupted manifest apply.
    with service.db.transaction() as transaction:
        service.store.begin_manifest_apply(
            transaction=transaction,
            evidence_id=evidence_id,
            digest="a" * 64,
            payload={"verdict": "approved"},
        )
    SessionManager(service.db).update_status(coordinator_id, "expired")

    expired = service.expire_plan_review_evidence(evidence_id, caller_session_id=reviewer_id)
    assert expired.manifest_state == "pending"
    assert expired.expired_at is not None
    successor = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=reviewer_id,
    )
    assert successor.evidence_id != evidence_id


def test_all_ended_static_seats_allow_live_project_successor(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    successor_id, _ = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    sessions = SessionManager(service.db)
    for seat in (reviewer_id, writer_id, coordinator_id):
        sessions.update_status(seat, "expired")

    foreign_project = isolated_checkout_factory(
        service.db, "foreign-recovery-seat", root=tmp_path / "foreign"
    ).project
    reviewer = sessions.get(reviewer_id)
    assert reviewer is not None
    foreign = sessions.register(
        external_id="foreign-recovery-caller",
        machine_id=reviewer.machine_id,
        source="codex",
        project_id=foreign_project.id,
    )
    with pytest.raises(ReviewEvidenceError) as foreign_caller:
        service.expire_plan_review_evidence(evidence_id, caller_session_id=foreign.id)
    assert foreign_caller.value.code == "unauthorized_seat"

    service.expire_plan_review_evidence(evidence_id, caller_session_id=successor_id)
    successor = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=successor_id,
    )
    assert successor.evidence_id != evidence_id


def test_static_bind_rejects_expired_and_superseded_attempts(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    old_id = _prepare(service, project_id, reviewer_id, plan_path)
    with service.db.transaction() as transaction:
        transaction.execute(
            "UPDATE plan_review_evidence SET lease_expires_at = NOW() - INTERVAL '1 second' "
            "WHERE evidence_id = %s",
            (old_id,),
        )
    new_id = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=reviewer_id,
    ).evidence_id
    assert new_id != old_id
    with pytest.raises(ReviewEvidenceError) as superseded:
        _bind(service, old_id, reviewer_id, writer_id, coordinator_id)
    assert superseded.value.code == "evidence_replay"
    service.expire_plan_review_evidence(new_id, spawn_failed=True)
    with pytest.raises(ReviewEvidenceError) as expired:
        _bind(service, new_id, reviewer_id, writer_id, coordinator_id)
    assert expired.value.code == "evidence_replay"


def test_static_and_run_bind_have_one_winner(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=reviewer_id, provider="codex", prompt="competing bind"
    )
    barrier = Barrier(2)

    def static_bind() -> str:
        barrier.wait()
        try:
            _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
        except ReviewEvidenceError as exc:
            return exc.code
        return "static"

    def run_bind() -> str:
        barrier.wait()
        try:
            service.bind_evidence_run(evidence_id, run.id)
        except ReviewEvidenceError as exc:
            return exc.code
        return "run"

    with ThreadPoolExecutor(max_workers=2) as executor:
        static_result = executor.submit(static_bind)
        run_result = executor.submit(run_bind)
        outcomes = {static_result.result(), run_result.result()}
    assert outcomes in ({"static", "evidence_already_bound"}, {"run", "evidence_already_bound"})
    bound = service.get_evidence(evidence_id)
    assert (bound.dispatch_run_id is None) != (bound.static_writer_session_id is None)
    assert bound.expired_at is None


def test_static_bind_rejects_session_from_another_project(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    _, coordinator_id = _seats(service.db, project_id, reviewer_id)
    foreign_project = isolated_checkout_factory(
        service.db, "foreign-review-seat", root=tmp_path / "foreign"
    ).project
    reviewer = SessionManager(service.db).get(reviewer_id)
    assert reviewer is not None
    foreign = SessionManager(service.db).register(
        external_id="foreign-static-writer",
        machine_id=reviewer.machine_id,
        source="codex",
        project_id=foreign_project.id,
    )
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    with pytest.raises(ReviewEvidenceError) as wrong_project:
        _bind(service, evidence_id, reviewer_id, foreign.id, coordinator_id)
    assert wrong_project.value.code == "invalid_seats"


def test_static_rejection_role_matrix_and_checkpoint(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    result = needs_review_result(evidence_id)

    for caller in (None, reviewer_id, coordinator_id):
        with pytest.raises(ReviewEvidenceError) as unauthorized:
            service.append_plan_changelog_round(
                evidence_id, "**Round 1**", result, caller_session_id=caller
            )
        assert unauthorized.value.code == "unauthorized_seat"
    appended = service.append_plan_changelog_round(
        evidence_id, "**Round 1**", result, caller_session_id=writer_id
    )
    assert appended["applied"] is True
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    with pytest.raises(ReviewEvidenceError) as unfinished:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=reviewer_id,
        )
    assert unfinished.value.code == "review_round_active"
    assert service.get_evidence(evidence_id).finalized_at is None
    for caller in (None, writer_id, coordinator_id):
        with pytest.raises(ReviewEvidenceError) as unauthorized:
            service.finalize_plan_review_evidence(evidence_id, result, caller_session_id=caller)
        assert unauthorized.value.code == "unauthorized_seat"
    finalized = service.finalize_plan_review_evidence(
        evidence_id, result, caller_session_id=reviewer_id
    )
    assert finalized.finalized_at is not None
    with pytest.raises(ReviewEvidenceError) as unauthorized:
        service.apply_plan_review_repairs(evidence_id, [], caller_session_id=coordinator_id)
    assert unauthorized.value.code == "unauthorized_seat"
    assert (
        service.apply_plan_review_repairs(evidence_id, [], caller_session_id=writer_id)["changed"]
        is False
    )


def test_static_approval_applies_manifest_before_checkpoint_and_finalize(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
) -> None:
    service, project_id, reviewer_id, plan_path = review_setup
    writer_id, coordinator_id = _seats(service.db, project_id, reviewer_id)
    evidence_id = _prepare(service, project_id, reviewer_id, plan_path)
    _bind(service, evidence_id, reviewer_id, writer_id, coordinator_id)
    derived = service.derive_plan_review_manifest(evidence_id, routing_decisions={})
    entries = derived["manifest_entries"]
    assert isinstance(entries, list)
    result: dict[str, object] = {
        "verdict": "approved",
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id, manifest_entries=entries
        ),
    }
    for caller in (None, writer_id, reviewer_id):
        with pytest.raises(ReviewEvidenceError) as unauthorized:
            service.apply_plan_review_manifest(
                evidence_id,
                result,
                plan_path=plan_path,
                run_id=None,
                caller_session_id=caller,
            )
        assert unauthorized.value.code == "unauthorized_seat"
    with pytest.raises(ReviewEvidenceError) as fake_run:
        service.apply_plan_review_manifest(
            evidence_id,
            result,
            plan_path=plan_path,
            run_id=str(uuid.uuid4()),
            caller_session_id=coordinator_id,
        )
    assert fake_run.value.code == "unauthorized_seat"
    applied = service.apply_plan_review_manifest(
        evidence_id,
        result,
        plan_path=plan_path,
        run_id=None,
        caller_session_id=coordinator_id,
    )
    assert applied["applied"] is True
    with pytest.raises(ReviewEvidenceError) as unfinished:
        service.prepare_plan_review_round(
            project_id=project_id,
            plan_path=plan_path,
            round_number=2,
            session_id=reviewer_id,
        )
    assert unfinished.value.code == "review_round_active"
    assert service.get_evidence(evidence_id).finalized_at is None
    appended = service.append_plan_changelog_round(
        evidence_id, "**Round 1**", result, caller_session_id=writer_id
    )
    assert appended["applied"] is True
    finalized = service.finalize_plan_review_evidence(
        evidence_id, result, caller_session_id=reviewer_id
    )
    assert finalized.approval_result == result
    with pytest.raises(ReviewEvidenceError) as unauthorized_mint:
        service.checkpoint_plan_review_lesson_mint(
            evidence_id, status="none", detail={}, caller_session_id=writer_id
        )
    assert unauthorized_mint.value.code == "unauthorized_seat"
    minted = service.checkpoint_plan_review_lesson_mint(
        evidence_id, status="none", detail={}, caller_session_id=reviewer_id
    )
    assert minted.lesson_mint_status == "none"
