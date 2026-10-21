from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.plans.review_evidence_models import PlanReviewEvidence
from tests.review_coverage_helpers import coverage_attestation
from tests.storage.stage_review_helpers import _prepare_bound
from tests.storage.stage_review_helpers import (
    stage_review_setup as _stage_review_setup,  # noqa: F401 - pytest discovers imported fixtures
)

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_approval_evidence_finalization(
    stage_review_setup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.plans.review_evidence_models import ReviewEvidenceError
    from gobby.plans.review_evidence_store import PlanReviewEvidenceStore

    setup = stage_review_setup
    evidence_id, run_id = _prepare_bound(setup)
    derived = setup.evidence.derive_plan_review_manifest(
        evidence_id,
        routing_decisions={},
    )
    manifest_entries = derived["manifest_entries"]
    assert isinstance(manifest_entries, list)
    approval = {
        "findings": [],
        "routing_decisions": {},
        "manifest_entries": manifest_entries,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            manifest_entries=manifest_entries,
        ),
    }

    with pytest.raises(ReviewEvidenceError) as wrong_round:
        setup.manager.approve_review(
            setup.task_id,
            "planning",
            evidence_id=evidence_id,
            round_number=2,
            **approval,
            dispatch_run_id=run_id,
        )
    assert wrong_round.value.code == "wrong_attempt"
    assert setup.manager.stage_states.current_stage(setup.task_id).state == "needs_review"

    original_finalize = PlanReviewEvidenceStore.finalize

    def crash_finalize(self: PlanReviewEvidenceStore, **_kwargs: object) -> PlanReviewEvidence:
        raise RuntimeError("crash inside approval commit")

    monkeypatch.setattr(PlanReviewEvidenceStore, "finalize", crash_finalize)
    with pytest.raises(RuntimeError, match="crash inside approval commit"):
        setup.manager.approve_review(
            setup.task_id,
            "planning",
            evidence_id=evidence_id,
            round_number=1,
            **approval,
            dispatch_run_id=run_id,
        )
    assert setup.manager.stage_states.current_stage(setup.task_id).state == "needs_review"
    interrupted = setup.evidence.get_evidence(evidence_id)
    assert interrupted.manifest_state is None
    assert interrupted.finalized_at is None

    monkeypatch.setattr(PlanReviewEvidenceStore, "finalize", original_finalize)
    approved = setup.manager.approve_review(
        setup.task_id,
        "planning",
        evidence_id=evidence_id,
        round_number=1,
        **approval,
        dispatch_run_id=run_id,
    )
    finalized = setup.evidence.get_evidence(evidence_id)
    assert approved.id == setup.task_id
    assert setup.manager.stage_states.current_stage(setup.task_id).state == "review_approved"
    assert finalized.finalized_at is not None
    assert finalized.approval_result == {"verdict": "approved", **approval}
    assert finalized.lesson_mint_status == "pending"

    replay = setup.manager.approve_review(
        setup.task_id,
        "planning",
        evidence_id=evidence_id,
        round_number=1,
        **approval,
        dispatch_run_id=run_id,
    )
    assert replay.id == setup.task_id
    assert setup.evidence.get_evidence(evidence_id).approved_at == finalized.approved_at
