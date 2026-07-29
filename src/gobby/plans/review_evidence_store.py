"""PostgreSQL persistence for plan-review evidence lifecycle state."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence

from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError, SectionHash
from gobby.storage.hub.protocol import HubDatabase, Transaction


class PlanReviewEvidenceStore:
    """Typed access to the daemon-owned plan_review_evidence table."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def get(
        self,
        evidence_id: str,
        *,
        transaction: Transaction | None = None,
        for_update: bool = False,
    ) -> PlanReviewEvidence | None:
        executor = transaction or self.db
        suffix = " FOR UPDATE" if for_update else ""
        row = executor.execute(
            f"SELECT * FROM plan_review_evidence WHERE evidence_id = %s{suffix}",  # nosec B608
            (evidence_id,),
        ).fetchone()
        return PlanReviewEvidence.from_row(row) if row is not None else None

    def get_by_dispatch_run(self, run_id: str) -> PlanReviewEvidence | None:
        """Return the evidence row bound to one dispatched reviewer run."""
        row = self.db.execute(
            "SELECT * FROM plan_review_evidence WHERE dispatch_run_id = %s",
            (run_id,),
        ).fetchone()
        return PlanReviewEvidence.from_row(row) if row is not None else None

    def require(
        self,
        evidence_id: str,
        *,
        transaction: Transaction | None = None,
        for_update: bool = False,
    ) -> PlanReviewEvidence:
        row = self.get(
            evidence_id,
            transaction=transaction,
            for_update=for_update,
        )
        if row is None:
            raise ReviewEvidenceError(
                "evidence_not_found",
                f"plan review evidence not found: {evidence_id}",
            )
        return row

    def insert(
        self,
        *,
        transaction: Transaction,
        project_id: str,
        plan_path: str,
        plan_hash: str,
        sections: tuple[SectionHash, ...],
        snapshot: bytes,
        round_number: int,
        lease_seconds: int,
        session_id: str | None,
        task_id: str | None,
        stage: str | None,
    ) -> PlanReviewEvidence:
        evidence_id = str(uuid.uuid4())
        row = transaction.execute(
            """
            INSERT INTO plan_review_evidence (
                evidence_id, project_id, plan_path, plan_hash, section_manifest,
                snapshot, round_number, session_id, task_id, stage,
                lease_expires_at
            )
            VALUES (
                %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                NOW() + (%s * INTERVAL '1 second')
            )
            RETURNING *
            """,
            (
                evidence_id,
                project_id,
                plan_path,
                plan_hash,
                json.dumps([section.to_dict() for section in sections]),
                snapshot,
                round_number,
                session_id,
                task_id,
                stage,
                lease_seconds,
            ),
        ).fetchone()
        if row is None:  # pragma: no cover - PostgreSQL RETURNING always yields one row.
            raise RuntimeError("plan_review_evidence insert returned no row")
        return PlanReviewEvidence.from_row(row)

    def list_for_path(
        self,
        *,
        project_id: str,
        plan_path: str,
        transaction: Transaction | None = None,
        for_update: bool = False,
    ) -> list[PlanReviewEvidence]:
        executor = transaction or self.db
        suffix = " FOR UPDATE" if for_update else ""
        rows = executor.execute(
            f"""
            SELECT *
            FROM plan_review_evidence
            WHERE project_id = %s AND plan_path = %s
            ORDER BY round_number, created_at
            {suffix}
            """,  # nosec B608
            (project_id, plan_path),
        ).fetchall()
        return [PlanReviewEvidence.from_row(row) for row in rows]

    def list_for_task_stage(
        self,
        *,
        task_id: str,
        stage: str,
    ) -> list[PlanReviewEvidence]:
        """Return finalized evidence rows that form one stage-native lineage."""
        rows = self.db.execute(
            """
            SELECT *
            FROM plan_review_evidence
            WHERE task_id = %s
              AND stage = %s
              AND finalized_at IS NOT NULL
              AND expired_at IS NULL
            ORDER BY round_number, created_at, evidence_id
            """,
            (task_id, stage),
        ).fetchall()
        return [PlanReviewEvidence.from_row(row) for row in rows]

    def active_for_path(
        self,
        *,
        project_id: str,
        plan_path: str,
        transaction: Transaction,
    ) -> PlanReviewEvidence | None:
        row = transaction.execute(
            """
            SELECT *
            FROM plan_review_evidence
            WHERE project_id = %s
              AND plan_path = %s
              AND finalized_at IS NULL
              AND expired_at IS NULL
            FOR UPDATE
            """,
            (project_id, plan_path),
        ).fetchone()
        return PlanReviewEvidence.from_row(row) if row is not None else None

    def bind_run(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
        run_id: str,
    ) -> PlanReviewEvidence:
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET dispatch_run_id = %s, lease_expires_at = NULL
            WHERE evidence_id = %s
              AND finalized_at IS NULL
              AND expired_at IS NULL
              AND dispatch_run_id IS NULL
            RETURNING *
            """,
            (run_id, evidence_id),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        current = self.require(evidence_id, transaction=transaction, for_update=True)
        if current.dispatch_run_id == run_id:
            return current
        if not current.is_live:
            raise ReviewEvidenceError(
                "evidence_replay",
                f"evidence row is no longer live: {evidence_id}",
            )
        raise ReviewEvidenceError(
            "evidence_already_bound",
            f"evidence {evidence_id} is already bound to another run",
        )

    def expire(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
    ) -> PlanReviewEvidence:
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET expired_at = NOW()
            WHERE evidence_id = %s
              AND finalized_at IS NULL
              AND expired_at IS NULL
            RETURNING *
            """,
            (evidence_id,),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        current = self.require(evidence_id, transaction=transaction, for_update=True)
        if current.expired_at is not None:
            return current
        raise ReviewEvidenceError(
            "evidence_finalized",
            f"finalized evidence cannot be expired: {evidence_id}",
        )

    def finalize(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
        round_result: Mapping[str, object],
        approval_result: Mapping[str, object] | None,
    ) -> PlanReviewEvidence:
        payload = json.dumps(round_result, sort_keys=True, separators=(",", ":"))
        approval_payload = (
            json.dumps(approval_result, sort_keys=True, separators=(",", ":"))
            if approval_result is not None
            else None
        )
        approval = approval_result is not None
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET round_result = %s::jsonb,
                finalized_at = NOW(),
                approval_result = CASE WHEN %s THEN %s::jsonb ELSE approval_result END,
                approved_at = CASE WHEN %s THEN NOW() ELSE approved_at END,
                lesson_mint_status = CASE WHEN %s THEN 'pending' ELSE lesson_mint_status END
            WHERE evidence_id = %s
              AND finalized_at IS NULL
              AND expired_at IS NULL
              AND (round_result IS NULL OR round_result = %s::jsonb)
            RETURNING *
            """,
            (
                payload,
                approval,
                approval_payload,
                approval,
                approval,
                evidence_id,
                payload,
            ),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        current = self.require(evidence_id, transaction=transaction, for_update=True)
        if current.expired_at is not None:
            raise ReviewEvidenceError("evidence_replay", "expired evidence cannot be finalized")
        if current.round_result != dict(round_result):
            raise ReviewEvidenceError(
                "round_result_conflict",
                f"round result conflicts with durable evidence intent: {evidence_id}",
            )
        return current

    def write_preparation_context(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
        repair_attestations: Sequence[Mapping[str, object]],
        prior_round_context: Mapping[str, object],
    ) -> PlanReviewEvidence:
        attestations = [dict(attestation) for attestation in repair_attestations]
        context = dict(prior_round_context)
        encoded_attestations = json.dumps(
            attestations,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded_context = json.dumps(context, sort_keys=True, separators=(",", ":"))
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET repair_attestations = %s::jsonb,
                prior_round_context = %s::jsonb
            WHERE evidence_id = %s
              AND finalized_at IS NULL
              AND expired_at IS NULL
              AND (
                  (repair_attestations IS NULL AND prior_round_context IS NULL)
                  OR (
                      repair_attestations = %s::jsonb
                      AND prior_round_context = %s::jsonb
                  )
              )
            RETURNING *
            """,
            (
                encoded_attestations,
                encoded_context,
                evidence_id,
                encoded_attestations,
                encoded_context,
            ),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        current = self.require(evidence_id, transaction=transaction, for_update=True)
        if current.finalized_at is not None or current.expired_at is not None:
            raise ReviewEvidenceError(
                "preparation_context_closed",
                f"preparation context cannot be written to closed evidence: {evidence_id}",
            )
        if current.repair_attestations == attestations and current.prior_round_context == context:
            return current
        raise ReviewEvidenceError(
            "preparation_context_conflict",
            f"preparation context conflicts with durable evidence intent: {evidence_id}",
        )

    def write_quality_ledger(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
        quality_ledger: Sequence[Mapping[str, object]],
    ) -> PlanReviewEvidence:
        ledger = [dict(entry) for entry in quality_ledger]
        encoded = json.dumps(ledger, sort_keys=True, separators=(",", ":"))
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET quality_ledger = %s::jsonb
            WHERE evidence_id = %s
              AND finalized_at IS NULL
              AND expired_at IS NULL
              AND (quality_ledger IS NULL OR quality_ledger = %s::jsonb)
            RETURNING *
            """,
            (encoded, evidence_id, encoded),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        current = self.require(evidence_id, transaction=transaction, for_update=True)
        if current.quality_ledger == ledger:
            return current
        raise ReviewEvidenceError(
            "quality_ledger_conflict",
            f"quality ledger conflicts with durable evidence intent: {evidence_id}",
        )

    def pending_interactive_mints(
        self,
        *,
        project_id: str,
        plan_path: str,
        session_id: str,
        transaction: Transaction,
    ) -> list[PlanReviewEvidence]:
        rows = transaction.execute(
            """
            SELECT *
            FROM plan_review_evidence
            WHERE project_id = %s
              AND plan_path = %s
              AND session_id = %s
              AND finalized_at IS NOT NULL
              AND lesson_mint_status = 'pending'
            ORDER BY round_number, created_at
            FOR UPDATE
            """,
            (project_id, plan_path, session_id),
        ).fetchall()
        return [PlanReviewEvidence.from_row(row) for row in rows]

    def begin_manifest_apply(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
        digest: str,
        payload: Mapping[str, object],
    ) -> PlanReviewEvidence:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET manifest_digest = %s,
                manifest_payload = %s::jsonb,
                manifest_state = 'pending',
                round_result = %s::jsonb
            WHERE evidence_id = %s
              AND finalized_at IS NULL
              AND expired_at IS NULL
              AND manifest_state IS NULL
              AND round_result IS NULL
            RETURNING *
            """,
            (digest, encoded, encoded, evidence_id),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        return self.require(evidence_id, transaction=transaction, for_update=True)

    def complete_manifest_apply(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
        result: Mapping[str, object],
    ) -> PlanReviewEvidence:
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET manifest_state = 'applied',
                manifest_result = %s::jsonb,
                manifest_applied_at = NOW()
            WHERE evidence_id = %s
              AND manifest_state = 'pending'
              AND finalized_at IS NULL
              AND expired_at IS NULL
            RETURNING *
            """,
            (encoded, evidence_id),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        return self.require(evidence_id, transaction=transaction, for_update=True)

    def revoke_manifest_intent(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
    ) -> PlanReviewEvidence:
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET manifest_state = 'revoked',
                round_result = NULL
            WHERE evidence_id = %s
              AND manifest_state = 'pending'
              AND finalized_at IS NULL
              AND expired_at IS NULL
            RETURNING *
            """,
            (evidence_id,),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        return self.require(evidence_id, transaction=transaction, for_update=True)

    def checkpoint_mint(
        self,
        *,
        transaction: Transaction,
        evidence_id: str,
        status: str,
        detail: Mapping[str, object],
    ) -> PlanReviewEvidence:
        encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"))
        row = transaction.execute(
            """
            UPDATE plan_review_evidence
            SET lesson_mint_status = %s,
                lesson_mint_detail = %s::jsonb
            WHERE evidence_id = %s
              AND lesson_mint_status IN ('pending', 'failed')
              AND finalized_at IS NOT NULL
              AND approval_result IS NOT NULL
              AND (session_id IS NOT NULL OR task_id IS NOT NULL)
            RETURNING *
            """,
            (status, encoded, evidence_id),
        ).fetchone()
        if row is not None:
            return PlanReviewEvidence.from_row(row)
        current = self.require(evidence_id, transaction=transaction, for_update=True)
        if current.lesson_mint_status == status and current.lesson_mint_detail == dict(detail):
            return current
        if current.lesson_mint_status in {"minted", "none"}:
            raise ReviewEvidenceError(
                "lesson_mint_conflict",
                f"lesson mint already checkpointed as {current.lesson_mint_status}",
            )
        raise ReviewEvidenceError(
            "invalid_lesson_mint_state",
            "lesson mint checkpoint requires a finalized approval row",
        )
