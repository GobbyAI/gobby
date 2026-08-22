"""Interactive checkpoint and lesson-mint lifecycle for plan review."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from gobby.plans.review_evidence_io import (
    append_round_entry,
    atomic_write_bytes,
    ensure_checkpoint,
    parse_checkpoints,
    render_checkpoint,
    render_manifest_plan,
)
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    validate_round_result,
)
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.storage.hub.protocol import HubDatabase, PlanReviewEvidenceMutation, Transaction


class PlanUnchangedVerifier(Protocol):
    """Verify that a plan still matches its evidence snapshot."""

    def __call__(
        self,
        evidence_id: str,
        plan_path: str | Path,
    ) -> bool: ...


class ReviewCheckpointService:
    """Own interactive checkpoints, reconciliation, and lesson-mint state."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        store: PlanReviewEvidenceStore,
    ) -> None:
        self.db = db
        self.store = store

    def render_plan_changelog_round(
        self,
        evidence_id: str,
        round_result: Mapping[str, object] | None = None,
    ) -> bytes:
        evidence = self.store.require(evidence_id)
        if evidence.session_id is None:
            raise ReviewEvidenceError(
                "not_interactive_evidence",
                "V1 checkpoints are only valid for interactive evidence",
            )
        if round_result is None:
            if evidence.round_result is None:
                raise ReviewEvidenceError(
                    "missing_round_result",
                    "round_result is required until a durable approval intent exists",
                )
            payload = evidence.round_result
        else:
            payload = self.round_result_for_evidence(evidence_id, round_result)
            if evidence.round_result is not None and evidence.round_result != payload:
                raise ReviewEvidenceError(
                    "round_result_conflict",
                    "supplied round result conflicts with durable evidence intent",
                )
        return render_checkpoint(
            evidence_id=evidence.evidence_id,
            round_number=evidence.round_number,
            plan_hash=evidence.plan_hash,
            session_id=evidence.session_id,
            round_result=payload,
        )

    def append_plan_changelog_round(
        self,
        evidence_id: str,
        prose: str,
        round_result: Mapping[str, object] | None = None,
        *,
        plan_path: Path,
    ) -> dict[str, object]:
        """Render the canonical round fence and atomically insert prose + fence."""
        checkpoint = self.render_plan_changelog_round(evidence_id, round_result)
        applied = append_round_entry(plan_path, prose, checkpoint)
        return {
            "evidence_id": evidence_id,
            "applied": applied,
            "checkpoint": checkpoint.decode("utf-8"),
        }

    def require_durable_checkpoint(
        self,
        evidence: PlanReviewEvidence,
        round_result: Mapping[str, object],
        *,
        plan_path: Path,
    ) -> None:
        if not evidence.is_interactive:
            return
        plan_bytes = plan_path.read_bytes()
        matching = [
            checkpoint
            for checkpoint in parse_checkpoints(plan_bytes)
            if checkpoint["evidence_id"] == evidence.evidence_id
        ]
        expected = self.render_plan_changelog_round(evidence.evidence_id, round_result)
        if not matching or expected not in plan_bytes:
            raise ReviewEvidenceError(
                "missing_v1_checkpoint",
                "interactive finalization requires the durable V1 checkpoint",
            )

    def checkpoint_plan_review_lesson_mint(
        self,
        evidence_id: str,
        *,
        status: str,
        detail: Mapping[str, object],
    ) -> PlanReviewEvidence:
        if status not in {"minted", "failed", "none"}:
            raise ReviewEvidenceError(
                "invalid_lesson_mint_status",
                "status must be minted, failed, or none",
            )
        evidence = self.store.require(evidence_id)
        if (
            (evidence.session_id is None and evidence.task_id is None)
            or evidence.finalized_at is None
            or evidence.round_result is None
            or evidence.round_result.get("verdict") != "approved"
        ):
            raise ReviewEvidenceError(
                "invalid_lesson_mint_state",
                "lesson mint checkpoint requires a finalized approval row",
            )
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            return self.store.checkpoint_mint(
                transaction=transaction,
                evidence_id=evidence_id,
                status=status,
                detail=detail,
            )

    def reconcile_checkpoints(
        self,
        *,
        transaction: Transaction,
        project_id: str,
        plan_path: str,
        checkpoints: tuple[dict[str, object], ...],
    ) -> None:
        if not checkpoints:
            return
        for checkpoint in checkpoints:
            evidence_id = str(checkpoint["evidence_id"])
            try:
                evidence = self.store.require(
                    evidence_id,
                    transaction=transaction,
                    for_update=True,
                )
            except ReviewEvidenceError as exc:
                raise ReviewEvidenceError(
                    "checkpoint_reconciliation_error",
                    f"checkpoint references unresolved evidence {evidence_id}",
                ) from exc
            # Lineage binds the fence to the durable row, not to the caller:
            # any session may drain checkpoints left by an expired session.
            # The session comparison guards the finalize-from-fence path below
            # against a forged fence; a finalized row has nothing left to
            # finalize, and session-identity unification may have re-keyed it
            # onto a successor session after its fence was rendered.
            session_matches = evidence.session_id == checkpoint["session_id"]
            lineage_matches = (
                evidence.project_id == project_id
                and evidence.plan_path == plan_path
                and (session_matches or evidence.finalized_at is not None)
                and evidence.round_number == checkpoint["round_number"]
                and evidence.plan_hash == checkpoint["plan_hash"]
            )
            if not lineage_matches:
                raise ReviewEvidenceError(
                    "checkpoint_reconciliation_error",
                    f"checkpoint lineage mismatch for evidence {evidence_id}",
                )
            payload = checkpoint["round_result"]
            if not isinstance(payload, dict):
                raise ReviewEvidenceError(
                    "checkpoint_reconciliation_error",
                    f"checkpoint result is invalid for evidence {evidence_id}",
                )
            if evidence.round_result is not None and evidence.round_result != payload:
                raise ReviewEvidenceError(
                    "checkpoint_reconciliation_error",
                    f"checkpoint result conflicts for evidence {evidence_id}",
                )
            if evidence.finalized_at is None:
                self.finalize_evidence(
                    transaction=transaction,
                    evidence=evidence,
                    payload=payload,
                )

    def drain_interactive_intent(
        self,
        *,
        transaction: Transaction,
        evidence: PlanReviewEvidence,
        plan_path: Path,
        verify_plan_unchanged: PlanUnchangedVerifier,
    ) -> None:
        round_result = evidence.round_result
        if round_result is None:
            return
        if evidence.manifest_state == "pending":
            try:
                verify_plan_unchanged(evidence.evidence_id, plan_path)
            except ReviewEvidenceError:
                self.store.revoke_manifest_intent(
                    transaction=transaction,
                    evidence_id=evidence.evidence_id,
                )
                return
            entries = round_result.get("manifest_entries")
            if not isinstance(entries, list):
                raise ReviewEvidenceError(
                    "checkpoint_reconciliation_error",
                    "durable approval intent has no manifest entries",
                )
            rendered = render_manifest_plan(plan_path, plan_path.read_bytes(), entries)
            atomic_write_bytes(plan_path, rendered)
            evidence = self.store.complete_manifest_apply(
                transaction=transaction,
                evidence_id=evidence.evidence_id,
                result={
                    "evidence_id": evidence.evidence_id,
                    "manifest_digest": evidence.manifest_digest,
                    "applied": True,
                },
            )
        if evidence.manifest_state != "applied":
            return
        checkpoint = self.render_plan_changelog_round(evidence.evidence_id)
        ensure_checkpoint(plan_path, checkpoint)
        self.finalize_evidence(
            transaction=transaction,
            evidence=evidence,
            payload=round_result,
        )

    def finalize_evidence(
        self,
        *,
        transaction: Transaction,
        evidence: PlanReviewEvidence,
        payload: Mapping[str, object],
    ) -> PlanReviewEvidence:
        """Persist one validated round result."""
        return self.store.finalize(
            transaction=transaction,
            evidence_id=evidence.evidence_id,
            round_result=payload,
            approval=payload["verdict"] == "approved",
        )

    @staticmethod
    def round_result_for_evidence(
        evidence_id: str,
        round_result: Mapping[str, object],
    ) -> dict[str, object]:
        payload = validate_round_result(round_result)
        coverage = payload["coverage_attestation"]
        result_evidence_id = coverage.get("evidence_id") if isinstance(coverage, dict) else None
        if result_evidence_id != evidence_id:
            raise ReviewEvidenceError(
                "coverage_evidence_mismatch",
                "round result belongs to a different review evidence snapshot",
            )
        return payload
