"""Static review seat binding, authorization, and attempt expiry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.plans.review_manifest_service import ReviewManifestService
from gobby.storage.hub.protocol import HubDatabase, PlanReviewEvidenceMutation
from gobby.storage.sessions import LIVE_SESSION_STATUSES


class ReviewAttemptService:
    """Own the authority and lifetime of one static review attempt."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        store: PlanReviewEvidenceStore,
        manifests: ReviewManifestService,
    ) -> None:
        self.db = db
        self.store = store
        self.manifests = manifests

    def bind_static_review_seats(
        self,
        evidence_id: str,
        *,
        writer_session_id: str,
        coordinator_session_id: str,
        caller_session_id: str | None,
        plan_path: str | Path | None = None,
    ) -> PlanReviewEvidence:
        """Bind one live interactive attempt to three exact, persisted session seats."""
        evidence = self.store.require(evidence_id)
        if caller_session_id is None or caller_session_id != evidence.session_id:
            raise ReviewEvidenceError("unauthorized_seat", "only the evidence owner may bind seats")
        if not evidence.is_interactive:
            raise ReviewEvidenceError(
                "not_interactive_evidence", "static seats require an interactive round"
            )
        try:
            writer_session_id = str(uuid.UUID(writer_session_id))
            coordinator_session_id = str(uuid.UUID(coordinator_session_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ReviewEvidenceError(
                "invalid_seats", "review seats require session UUIDs"
            ) from exc
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            locked = self.store.require(evidence_id, transaction=transaction, for_update=True)
            if not locked.is_live:
                raise ReviewEvidenceError("evidence_replay", "evidence row is no longer live")
            if locked.is_static_bound:
                if (
                    locked.static_writer_session_id == writer_session_id
                    and locked.static_coordinator_session_id == coordinator_session_id
                ):
                    return locked
                raise ReviewEvidenceError("evidence_already_bound", "static seats are immutable")
            active = self.store.active_for_path(
                project_id=locked.project_id,
                plan_path=locked.plan_path,
                transaction=transaction,
            )
            if active is None or active.evidence_id != evidence_id:
                raise ReviewEvidenceError(
                    "wrong_attempt", "evidence is not the current review attempt"
                )
            seats = {caller_session_id, writer_session_id, coordinator_session_id}
            if len(seats) != 3:
                raise ReviewEvidenceError("invalid_seats", "review seats must be distinct sessions")
            rows = transaction.execute(
                "SELECT id, project_id, status FROM sessions "
                "WHERE id IN (%s, %s, %s) ORDER BY id FOR UPDATE",
                (caller_session_id, writer_session_id, coordinator_session_id),
            ).fetchall()
            if len(rows) != 3 or any(
                str(row["project_id"]) != locked.project_id
                or row["status"] not in LIVE_SESSION_STATUSES
                for row in rows
            ):
                raise ReviewEvidenceError(
                    "invalid_seats", "review seats must be live sessions in the project"
                )
            source_path = plan_path or locked.source_plan_path
            if source_path is None or (
                locked.source_plan_path is None and not Path(source_path).is_absolute()
            ):
                raise ReviewEvidenceError(
                    "invalid_plan_path", "legacy evidence requires an absolute plan_path"
                )
            resolved = self.manifests.resolve_evidence_path(locked, source_path)
            if resolved.read_bytes() != locked.snapshot:
                raise ReviewEvidenceError(
                    "stale_snapshot", "plan bytes differ from reviewed evidence"
                )
            return self.store.bind_static_seats(
                transaction=transaction,
                evidence_id=evidence_id,
                writer_session_id=writer_session_id,
                coordinator_session_id=coordinator_session_id,
                source_plan_path=str(resolved),
            )

    def expire_plan_review_evidence(
        self,
        evidence_id: str,
        *,
        spawn_failed: bool = False,
        caller_session_id: str | None = None,
    ) -> PlanReviewEvidence:
        evidence = self.store.require(evidence_id)
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            locked = self.store.require(evidence_id, transaction=transaction, for_update=True)
            if locked.is_static_bound:
                try:
                    caller_session_id = str(uuid.UUID(caller_session_id or ""))
                except (TypeError, ValueError) as exc:
                    raise ReviewEvidenceError(
                        "unauthorized_seat", "live project session required"
                    ) from exc
                seats = {
                    locked.session_id,
                    locked.static_writer_session_id,
                    locked.static_coordinator_session_id,
                }
                rows = transaction.execute(
                    "SELECT id, project_id, status FROM sessions "
                    "WHERE id IN (%s, %s, %s, %s) ORDER BY id FOR UPDATE",
                    (*seats, caller_session_id),
                ).fetchall()
                sessions = {str(row["id"]): row for row in rows}
                caller = sessions.get(caller_session_id) if caller_session_id is not None else None
                if (
                    caller is None
                    or str(caller["project_id"]) != locked.project_id
                    or caller["status"] not in LIVE_SESSION_STATUSES
                ):
                    raise ReviewEvidenceError("unauthorized_seat", "live project session required")
                abandoned = {
                    seat
                    for seat in seats
                    if seat is not None
                    and (
                        seat not in sessions
                        or sessions[seat]["status"] not in LIVE_SESSION_STATUSES
                    )
                }
                if not abandoned:
                    raise ReviewEvidenceError(
                        "attempt_still_live", "all static review seats are live", retryable=True
                    )
                if caller_session_id not in seats and abandoned != seats:
                    raise ReviewEvidenceError("unauthorized_seat", "bound seat required")
                # The successor round snapshots the current plan bytes; retain this
                # abandoned row and its checkpoint or manifest intent for audit.
            else:
                if locked.round_result is not None and locked.manifest_state != "revoked":
                    raise ReviewEvidenceError(
                        "durable_result_present",
                        "evidence with a durable round result must be reconciled",
                    )
                if not spawn_failed and not self.attempt_is_dead(locked):
                    raise ReviewEvidenceError(
                        "attempt_still_live",
                        "evidence attempt is still live",
                        retryable=True,
                    )
            return self.store.expire(
                transaction=transaction,
                evidence_id=evidence_id,
            )

    def authorize_current_attempt(
        self,
        evidence_id: str,
        *,
        project_id: str,
        plan_path: str | Path,
        round_number: int,
        session_id: str | None = None,
        task_id: str | None = None,
        stage: str | None = None,
        caller_session_id: str | None = None,
        allow_rejection_replay: bool = False,
        allow_approval_replay: bool = False,
    ) -> PlanReviewEvidence:
        evidence = self.store.require(evidence_id)
        resolved, relative_path = self.manifests.resolve_plan_path(project_id, plan_path)
        token_matches = (
            evidence.project_id == project_id
            and evidence.plan_path == relative_path
            and (evidence.source_plan_path is None or str(resolved) == evidence.source_plan_path)
            and evidence.round_number == round_number
            and evidence.session_id == session_id
            and evidence.task_id == task_id
            and evidence.stage == stage
        )
        static_matches = (
            evidence.is_static_bound
            and caller_session_id is not None
            and caller_session_id == evidence.static_coordinator_session_id
        )
        if (
            allow_rejection_replay
            and evidence.finalized_at is not None
            and evidence.round_result is not None
            and evidence.round_result.get("verdict") == "needs_review"
            and token_matches
            and static_matches
        ):
            return evidence
        if (
            allow_approval_replay
            and evidence.finalized_at is not None
            and evidence.approval_result is not None
            and evidence.approval_result.get("verdict") == "approved"
            and token_matches
            and static_matches
        ):
            return evidence
        if not token_matches:
            raise ReviewEvidenceError(
                "wrong_attempt",
                "evidence does not belong to the current review attempt",
            )
        if not evidence.is_live:
            raise ReviewEvidenceError("evidence_replay", "evidence row is no longer live")
        if evidence.is_static_bound:
            if not static_matches:
                raise ReviewEvidenceError("unauthorized_seat", "coordinator seat required")
            return evidence
        raise ReviewEvidenceError(
            "binding_pending",
            "evidence static seat binding is pending",
            retryable=True,
        )

    def attempt_is_dead(self, evidence: PlanReviewEvidence) -> bool:
        if not evidence.is_live or evidence.is_static_bound:
            return False
        expires = evidence.lease_expires_at
        return expires is None or expires <= datetime.now(UTC)
