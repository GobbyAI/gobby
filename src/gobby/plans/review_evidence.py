"""Durable preparation and lifecycle service for plan-review evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from gobby.plans.parser import PlanDocument
from gobby.plans.review_checkpoint_service import ReviewCheckpointService
from gobby.plans.review_coverage import (
    review_complexity,
    validate_approval_condition,
    validate_review_coverage,
)
from gobby.plans.review_evidence_io import (
    DEFAULT_SNAPSHOT_PAGE_BYTES,
    build_section_manifest,
    normalize_plan_path,
    paginate_snapshot_envelope,
    parse_checkpoints,
    reviewed_section_hashes,
    serialize_snapshot_envelope,
)
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    PreparedReviewEvidence,
    ReviewEvidenceError,
)
from gobby.plans.review_evidence_preparation import prepare_review_round_context
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.plans.review_findings import validate_plan_review_findings
from gobby.plans.review_manifest_service import ReviewManifestService
from gobby.plans.review_requirements import (
    ANCHOR_TARGET_FIELD,
    REQUEST_ANCHOR_VARIABLE,
    assemble_requirements_bundle,
    is_plan_accept_anchor,
    plan_accept_anchor_matches,
    requirements_bundle_from_context,
)
from gobby.plans.review_telemetry import validate_convergence_telemetry
from gobby.plans.vote_artifacts import (
    COORDINATOR_PROVENANCE,
    PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
    build_coordinator_receipt,
    build_plan_vote_artifact,
    canonical_digest,
    require_vote_artifact_fold_in,
    validate_observer_receipt,
    validate_vote_attempt,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import (
    HubDatabase,
    PlanReviewEvidenceMutation,
    Transaction,
)
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.state_manager import SessionVariableManager

EVIDENCE_LEASE_SECONDS = 7_200


class PlanReviewEvidenceService:
    """Coordinate immutable snapshots with durable evidence lifecycle state."""

    def __init__(self, db: HubDatabase) -> None:
        # LocalTaskManager imports review transitions, which import this service.
        # Keep this delayed to break that cycle during module initialization.
        from gobby.storage.tasks import LocalTaskManager

        self.db = db
        self.store = PlanReviewEvidenceStore(db)
        self.projects = LocalProjectManager(db)
        self.tasks = LocalTaskManager(db)
        self.session_variables = SessionVariableManager(db)
        self.agent_runs = LocalAgentRunManager(db)
        self.checkpoints = ReviewCheckpointService(db=db, store=self.store)
        self.manifests = ReviewManifestService(
            db=db,
            store=self.store,
            projects=self.projects,
        )

    def prepare_plan_review_round(
        self,
        *,
        project_id: str,
        plan_path: str | Path,
        round_number: int,
        session_id: str | None = None,
        task_id: str | None = None,
        stage: str | None = None,
        prior_finding_resolutions: Sequence[Mapping[str, object]] | None = None,
        repair_attestations: Sequence[Mapping[str, object]] | None = None,
        sweep_scope: Mapping[str, object] | None = None,
        sweep_scope_digest: str | None = None,
    ) -> PreparedReviewEvidence:
        """Capture one immutable round snapshot under a per-plan mutation lock."""
        if round_number <= 0:
            raise ReviewEvidenceError(
                "invalid_round_number",
                "round_number must be a positive integer",
            )
        self._validate_attempt_binding(
            session_id=session_id,
            task_id=task_id,
            stage=stage,
        )
        project = self.projects.get(project_id)
        if project is None or project.repo_path is None:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {project_id}",
            )
        root = Path(project.repo_path)
        resolved = normalize_plan_path(root, plan_path)
        relative_path = resolved.relative_to(root.resolve(strict=True)).as_posix()
        mutation = PlanReviewEvidenceMutation(
            project_id=project_id,
            plan_path=relative_path,
        )
        prepared: PreparedReviewEvidence | None = None
        pending_payload: list[dict[str, object]] = []
        with self.db.transaction_immediate(mutation) as transaction:
            snapshot = resolved.read_bytes()
            checkpoints = parse_checkpoints(snapshot)
            self.checkpoints.reconcile_checkpoints(
                transaction=transaction,
                project_id=project_id,
                plan_path=relative_path,
                session_id=session_id,
                checkpoints=checkpoints,
            )
            active = self.store.active_for_path(
                project_id=project_id,
                plan_path=relative_path,
                transaction=transaction,
            )
            if (
                active is not None
                and active.is_interactive
                and active.vote_artifact is not None
                and active.round_result is None
            ):
                require_vote_artifact_fold_in(active, plan_bytes=resolved.read_bytes())
                self.store.expire(
                    transaction=transaction,
                    evidence_id=active.evidence_id,
                )
                snapshot = resolved.read_bytes()
                checkpoints = parse_checkpoints(snapshot)
                active = None
            if active is not None and active.is_interactive and active.round_result is not None:
                self.checkpoints.drain_interactive_intent(
                    transaction=transaction,
                    evidence=active,
                    plan_path=resolved,
                    verify_plan_unchanged=self.verify_plan_unchanged,
                )
                snapshot = resolved.read_bytes()
                checkpoints = parse_checkpoints(snapshot)
                active = self.store.active_for_path(
                    project_id=project_id,
                    plan_path=relative_path,
                    transaction=transaction,
                )
            if session_id is not None:
                pending_mints = self.store.pending_interactive_mints(
                    project_id=project_id,
                    plan_path=relative_path,
                    session_id=session_id,
                    transaction=transaction,
                )
                if pending_mints:
                    pending_payload = [
                        {
                            "evidence_id": row.evidence_id,
                            "round_number": row.round_number,
                            "round_result": row.round_result,
                        }
                        for row in pending_mints
                    ]
            if not pending_payload:
                if active is not None:
                    if self._matches_attempt(
                        active,
                        round_number=round_number,
                        session_id=session_id,
                        task_id=task_id,
                        stage=stage,
                    ):
                        if active.dispatch_run_id is not None:
                            raise ReviewEvidenceError(
                                "review_round_bound",
                                (
                                    f"plan review evidence {active.evidence_id} is already "
                                    f"bound to agent run {active.dispatch_run_id}"
                                ),
                                retryable=True,
                                details={
                                    "evidence_id": active.evidence_id,
                                    "run_id": active.dispatch_run_id,
                                },
                            )
                        context = prepare_review_round_context(
                            db=self.db,
                            project_id=project_id,
                            project_root=root,
                            evidence_rows=self.store.list_for_path(
                                project_id=project_id,
                                plan_path=relative_path,
                                transaction=transaction,
                            ),
                            round_number=round_number,
                            current_sections=active.section_manifest,
                            current_snapshot=active.snapshot,
                            prior_finding_resolutions=prior_finding_resolutions,
                            repair_attestations=repair_attestations,
                            sweep_scope=sweep_scope,
                            sweep_scope_digest=sweep_scope_digest,
                        )
                        requirements_bundle = requirements_bundle_from_context(
                            active.prior_round_context
                        ) or self._assemble_requirements_bundle(
                            project_id=project_id,
                            project_root=root,
                            snapshot=active.snapshot,
                            session_id=session_id,
                            task_id=task_id,
                            plan_path=relative_path,
                        )
                        preparation_context = dict(
                            context.prior_round_context
                            if context is not None
                            else active.prior_round_context or {}
                        )
                        preparation_context["requirements_bundle"] = requirements_bundle
                        active = self.store.write_preparation_context(
                            transaction=transaction,
                            evidence_id=active.evidence_id,
                            repair_attestations=(
                                context.repair_attestations
                                if context is not None
                                else active.repair_attestations or []
                            ),
                            prior_round_context=preparation_context,
                        )
                        prepared = active.prepared_result()
                    elif self._attempt_is_dead(active):
                        self.store.expire(
                            transaction=transaction,
                            evidence_id=active.evidence_id,
                        )
                        active = None
                if active is not None and prepared is None:
                    raise ReviewEvidenceError(
                        "review_round_active",
                        f"another plan review round is active for {relative_path}",
                        retryable=True,
                        details={"evidence_id": active.evidence_id},
                    )
                if prepared is None:
                    plan_hash = hashlib.sha256(snapshot).hexdigest()
                    sections = build_section_manifest(snapshot)
                    context = prepare_review_round_context(
                        db=self.db,
                        project_id=project_id,
                        project_root=root,
                        evidence_rows=self.store.list_for_path(
                            project_id=project_id,
                            plan_path=relative_path,
                            transaction=transaction,
                        ),
                        round_number=round_number,
                        current_sections=sections,
                        current_snapshot=snapshot,
                        prior_finding_resolutions=prior_finding_resolutions,
                        repair_attestations=repair_attestations,
                        sweep_scope=sweep_scope,
                        sweep_scope_digest=sweep_scope_digest,
                    )
                    requirements_bundle = self._assemble_requirements_bundle(
                        project_id=project_id,
                        project_root=root,
                        snapshot=snapshot,
                        session_id=session_id,
                        task_id=task_id,
                        plan_path=relative_path,
                    )
                    evidence = self.store.insert(
                        transaction=transaction,
                        project_id=project_id,
                        plan_path=relative_path,
                        plan_hash=plan_hash,
                        sections=sections,
                        snapshot=snapshot,
                        round_number=round_number,
                        lease_seconds=EVIDENCE_LEASE_SECONDS,
                        session_id=session_id,
                        task_id=task_id,
                        stage=stage,
                    )
                    preparation_context = dict(
                        context.prior_round_context if context is not None else {}
                    )
                    preparation_context["requirements_bundle"] = requirements_bundle
                    evidence = self.store.write_preparation_context(
                        transaction=transaction,
                        evidence_id=evidence.evidence_id,
                        repair_attestations=(
                            context.repair_attestations if context is not None else []
                        ),
                        prior_round_context=preparation_context,
                    )
                    prepared = evidence.prepared_result()
        if pending_payload:
            raise ReviewEvidenceError(
                "pending_lesson_mint",
                "interactive approval lessons must be checkpointed before another round",
                details={"pending": pending_payload},
            )
        if prepared is None:  # pragma: no cover - guarded by the branches above.
            raise RuntimeError("plan review preparation produced no result")
        return prepared

    def get_evidence(self, evidence_id: str) -> PlanReviewEvidence:
        return self.store.require(evidence_id)

    def record_observed_vote_artifact(
        self,
        *,
        evidence_id: str,
        caller_session_id: str,
        plan_path: str | Path,
        round_kind: str,
        round_number: int,
        interaction_tool: str,
        interaction_payload: Mapping[str, object],
        votes: Sequence[Mapping[str, object]],
        receipt: object,
    ) -> PlanReviewEvidence:
        """Validate and consume an observer receipt in the evidence transaction."""
        evidence = self.get_evidence(evidence_id)
        _, relative_path = self._resolve_plan_path(evidence.project_id, plan_path)
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            current = self.store.require(evidence_id, transaction=transaction, for_update=True)
            validate_vote_attempt(
                current,
                caller_session_id=caller_session_id,
                plan_path=relative_path,
                round_number=round_number,
            )
            artifact = build_plan_vote_artifact(
                evidence_id=evidence_id,
                project_id=current.project_id,
                session_id=caller_session_id,
                plan_path=relative_path,
                round_kind=round_kind,
                round_number=round_number,
                interaction_tool=interaction_tool,
                interaction_payload=interaction_payload,
                votes=votes,
            )
            artifact_votes = artifact.get("votes")
            if not isinstance(artifact_votes, list):  # pragma: no cover
                raise RuntimeError("canonical vote artifact omitted votes")
            canonical_receipt = validate_observer_receipt(
                receipt,
                evidence_id=evidence_id,
                round_number=round_number,
                round_kind=round_kind,
                content_sha256=current.plan_hash,
                captured_by=caller_session_id,
                interaction_tool=interaction_tool,
                interaction_payload=interaction_payload,
                votes=[vote for vote in artifact_votes if isinstance(vote, Mapping)],
            )
            return self.store.write_vote_artifact(
                transaction=transaction,
                evidence_id=evidence_id,
                artifact=artifact,
                artifact_digest=canonical_digest(artifact),
                receipt=canonical_receipt,
                receipt_digest=canonical_digest(canonical_receipt),
                consume_session_id=caller_session_id,
                receipt_variable=PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
            )

    def record_coordinator_decision(
        self,
        *,
        evidence_id: str,
        caller_session_id: str,
        round_kind: str,
        interaction_payload: Mapping[str, object],
        votes: Sequence[Mapping[str, object]],
    ) -> PlanReviewEvidence:
        """Persist a coordinator-authored vote after transport authentication."""
        evidence = self.get_evidence(evidence_id)
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            current = self.store.require(evidence_id, transaction=transaction, for_update=True)
            validate_vote_attempt(
                current,
                caller_session_id=caller_session_id,
                plan_path=current.plan_path,
                round_number=current.round_number,
            )
            artifact = build_plan_vote_artifact(
                evidence_id=evidence_id,
                project_id=current.project_id,
                session_id=caller_session_id,
                plan_path=current.plan_path,
                round_kind=round_kind,
                round_number=current.round_number,
                interaction_tool="coordinator_decision",
                interaction_payload=interaction_payload,
                votes=votes,
                provenance=COORDINATOR_PROVENANCE,
            )
            artifact_votes = artifact.get("votes")
            if not isinstance(artifact_votes, list):  # pragma: no cover
                raise RuntimeError("canonical vote artifact omitted votes")
            receipt = build_coordinator_receipt(
                evidence_id=evidence_id,
                round_number=current.round_number,
                round_kind=round_kind,
                content_sha256=current.plan_hash,
                captured_by=caller_session_id,
                votes=[vote for vote in artifact_votes if isinstance(vote, Mapping)],
            )
            return self.store.write_vote_artifact(
                transaction=transaction,
                evidence_id=evidence_id,
                artifact=artifact,
                artifact_digest=canonical_digest(artifact),
                receipt=receipt,
                receipt_digest=canonical_digest(receipt),
            )

    def snapshot_bytes(self, evidence_id: str) -> bytes:
        return self.get_evidence(evidence_id).snapshot

    def snapshot_payload(self, evidence_id: str) -> dict[str, object]:
        evidence = self.get_evidence(evidence_id)
        document = self._snapshot_document(evidence)
        changed_sections = self._changed_sections_since_prior_round(evidence)
        return {
            "evidence_id": evidence.evidence_id,
            "plan_hash": evidence.plan_hash,
            "sections": [section.to_dict() for section in evidence.section_manifest],
            "snapshot": evidence.snapshot,
            "changed_section_ids": changed_sections,
            "prior_round_context": evidence.prior_round_context,
            "review_complexity": review_complexity(
                document,
                changed_section_count=len(changed_sections),
            ),
        }

    def snapshot_page(
        self,
        evidence_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_SNAPSHOT_PAGE_BYTES,
    ) -> dict[str, object]:
        evidence = self.get_evidence(evidence_id)
        if not isinstance(evidence.snapshot, bytes):
            raise ReviewEvidenceError(
                "invalid_evidence_row",
                "stored plan snapshot is not bytes",
            )
        document = self._snapshot_document(evidence)
        changed_sections = self._changed_sections_since_prior_round(evidence)
        envelope = serialize_snapshot_envelope(
            evidence_id=evidence.evidence_id,
            plan_hash=evidence.plan_hash,
            round_number=evidence.round_number,
            snapshot=evidence.snapshot,
            section_manifest=evidence.section_manifest,
            changed_section_ids=changed_sections,
            prior_round_context=evidence.prior_round_context,
            quality_ledger=evidence.quality_ledger or (),
            review_complexity=review_complexity(
                document,
                changed_section_count=len(changed_sections),
            ),
        )
        return paginate_snapshot_envelope(envelope, offset=offset, limit=limit)

    def derive_plan_review_manifest(
        self,
        evidence_id: str,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        return self.manifests.derive_plan_review_manifest(
            evidence_id,
            routing_decisions,
        )

    def validate_plan_review_coverage(
        self,
        evidence_id: str,
        lane_results: list[object],
        candidate_dispositions: Mapping[str, object],
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate all research lanes and return a canonical coverage attestation."""
        evidence = self.get_evidence(evidence_id)
        routing = dict(routing_decisions)
        expected_shadow = self.derive_plan_review_manifest(evidence_id, routing)
        project = self.projects.get(evidence.project_id)
        if project is None or project.repo_path is None:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {evidence.project_id}",
            )
        return validate_review_coverage(
            evidence_id=evidence_id,
            project_root=Path(project.repo_path),
            document=self._snapshot_document(evidence),
            plan_hash=evidence.plan_hash,
            lane_results=lane_results,
            candidate_dispositions=candidate_dispositions,
            shadow_manifest_status=expected_shadow,
            expected_shadow_manifest_status=expected_shadow,
            prior_round_context=evidence.prior_round_context,
        )

    def verify_plan_unchanged(
        self,
        evidence_id: str,
        plan_path: str | Path,
    ) -> bool:
        evidence = self.get_evidence(evidence_id)
        resolved, relative_path = self._resolve_plan_path(evidence.project_id, plan_path)
        if relative_path != evidence.plan_path:
            raise ReviewEvidenceError(
                "wrong_plan",
                f"evidence belongs to {evidence.plan_path}, not {relative_path}",
            )
        self._verify_reviewed_bytes(evidence, resolved.read_bytes())
        return True

    @staticmethod
    def _verify_reviewed_bytes(
        evidence: PlanReviewEvidence,
        current_bytes: bytes,
    ) -> None:
        current = reviewed_section_hashes(build_section_manifest(current_bytes))
        captured = reviewed_section_hashes(evidence.section_manifest)
        if current != captured:
            changed = sorted(
                key for key in set(current) | set(captured) if current.get(key) != captured.get(key)
            )
            raise ReviewEvidenceError(
                "stale_plan_evidence",
                f"reviewed plan sections changed: {', '.join(changed)}",
                details={"changed_sections": changed},
            )

    def bind_evidence_run(self, evidence_id: str, run_id: str) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        run = self.agent_runs.get(run_id)
        if run is None:
            raise ReviewEvidenceError("run_not_found", f"agent run not found: {run_id}")
        if evidence.is_interactive:
            valid_lineage = run.parent_session_id == evidence.session_id and run.task_id is None
        else:
            valid_lineage = run.task_id == evidence.task_id
        if not valid_lineage:
            if evidence.dispatch_run_id is None and evidence.is_live:
                self.expire_plan_review_evidence(evidence_id, spawn_failed=True)
                if run.status in {"pending", "running"}:
                    self.agent_runs.cancel(
                        run.id,
                        result="plan review evidence bind failed",
                    )
            raise ReviewEvidenceError(
                "run_lineage_mismatch",
                "agent run does not belong to the evidence attempt",
            )
        if run.status not in {"pending", "running"}:
            raise ReviewEvidenceError(
                "run_not_active",
                f"agent run is already terminal: {run_id}",
            )
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        try:
            with self.db.transaction_immediate(mutation) as transaction:
                return self.store.bind_run(
                    transaction=transaction,
                    evidence_id=evidence_id,
                    run_id=run_id,
                )
        except ReviewEvidenceError:
            current = self.get_evidence(evidence_id)
            if run.status in {"pending", "running"}:
                self.agent_runs.cancel(
                    run.id,
                    result="plan review evidence bind failed",
                )
            if current.dispatch_run_id is None and current.is_live:
                self.expire_plan_review_evidence(evidence_id, spawn_failed=True)
            raise

    def expire_plan_review_evidence(
        self,
        evidence_id: str,
        *,
        spawn_failed: bool = False,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        if evidence.round_result is not None and evidence.manifest_state != "revoked":
            raise ReviewEvidenceError(
                "durable_result_present",
                "evidence with a durable round result must be reconciled",
            )
        explicit_prebind_failure = spawn_failed and evidence.dispatch_run_id is None
        if not explicit_prebind_failure and not self._attempt_is_dead(evidence):
            raise ReviewEvidenceError(
                "attempt_still_live",
                "evidence attempt is still live",
                retryable=True,
            )
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
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
        run_id: str | None = None,
        allow_rejection_replay: bool = False,
        allow_approval_replay: bool = False,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        _, relative_path = self._resolve_plan_path(project_id, plan_path)
        token_matches = (
            evidence.project_id == project_id
            and evidence.plan_path == relative_path
            and evidence.round_number == round_number
            and evidence.session_id == session_id
            and evidence.task_id == task_id
            and evidence.stage == stage
        )
        run_matches = evidence.dispatch_run_id == run_id and run_id is not None
        if (
            allow_rejection_replay
            and evidence.finalized_at is not None
            and evidence.round_result is not None
            and evidence.round_result.get("verdict") == "needs_review"
            and token_matches
            and run_matches
        ):
            return evidence
        if (
            allow_approval_replay
            and evidence.finalized_at is not None
            and evidence.approval_result is not None
            and evidence.approval_result.get("verdict") == "approved"
            and token_matches
            and run_matches
        ):
            return evidence
        if not token_matches:
            raise ReviewEvidenceError(
                "wrong_attempt",
                "evidence does not belong to the current review attempt",
            )
        if not evidence.is_live:
            raise ReviewEvidenceError("evidence_replay", "evidence row is no longer live")
        if evidence.dispatch_run_id is None:
            raise ReviewEvidenceError(
                "binding_pending",
                "evidence run binding is pending",
                retryable=True,
            )
        if not run_matches:
            raise ReviewEvidenceError(
                "wrong_attempt",
                "caller run does not own this evidence row",
            )
        return evidence

    def resolve_historical_proof(
        self,
        evidence_id: str,
        *,
        project_id: str,
        plan_path: str | Path,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        _, relative_path = self._resolve_plan_path(project_id, plan_path)
        same_lineage = (
            session_id is not None
            and evidence.session_id == session_id
            and evidence.task_id is None
        ) or (task_id is not None and evidence.task_id == task_id and evidence.session_id is None)
        if (
            evidence.project_id != project_id
            or evidence.plan_path != relative_path
            or not same_lineage
        ):
            raise ReviewEvidenceError(
                "historical_lineage_mismatch",
                "evidence is outside the requested plan lineage",
            )
        if evidence.finalized_at is None:
            raise ReviewEvidenceError(
                "historical_proof_incomplete",
                "unfinalized evidence is not historical proof",
            )
        return evidence

    def render_v1_round_checkpoint(
        self,
        evidence_id: str,
        round_result: Mapping[str, object] | None = None,
    ) -> bytes:
        return self.checkpoints.render_v1_round_checkpoint(
            evidence_id,
            round_result,
        )

    def finalize_plan_review_evidence(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        _derived_quality_ledger: Sequence[Mapping[str, object]] | None = None,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        payload = self._round_result_for_evidence(evidence_id, round_result)
        telemetry = payload.get("convergence_telemetry")
        if not isinstance(telemetry, dict):
            raise ReviewEvidenceError(
                "invalid_round_result",
                "round_result.convergence_telemetry must be an object",
            )
        validate_convergence_telemetry(telemetry, required_state="enriched")
        if evidence.round_result is not None and evidence.round_result != payload:
            raise ReviewEvidenceError(
                "round_result_conflict",
                "supplied round result conflicts with durable evidence intent",
            )
        if evidence.finalized_at is not None:
            if evidence.round_result == payload:
                return evidence
            raise ReviewEvidenceError("evidence_replay", "evidence is already finalized")
        if evidence.expired_at is not None:
            raise ReviewEvidenceError("evidence_replay", "evidence is expired")
        if evidence.is_interactive:
            self.checkpoints.require_durable_checkpoint(
                evidence,
                payload,
                plan_path=self._evidence_path(evidence),
            )
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            current = self.store.require(evidence_id, transaction=transaction, for_update=True)
            require_vote_artifact_fold_in(
                current,
                plan_bytes=self._evidence_path(current).read_bytes(),
            )
            return self.checkpoints.finalize_evidence(
                transaction=transaction,
                evidence=current,
                payload=payload,
                derived_quality_ledger=_derived_quality_ledger,
            )

    def derive_quality_ledger_for_evidence(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        transaction: Transaction,
    ) -> list[dict[str, object]]:
        """Derive the single server-owned ledger before any terminal stage mutation."""
        evidence = self.get_evidence(evidence_id)
        payload = self._round_result_for_evidence(evidence_id, round_result)
        return self.checkpoints.derive_quality_ledger(
            evidence=evidence,
            payload=payload,
            transaction=transaction,
        )

    def apply_plan_review_manifest(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        plan_path: str | Path,
        run_id: str | None,
    ) -> dict[str, object]:
        return self.manifests.apply_plan_review_manifest(
            evidence_id,
            round_result,
            plan_path=plan_path,
            run_id=run_id,
            resolve_round_result=self._round_result_for_evidence,
            authorize_attempt=self.authorize_current_attempt,
            verify_reviewed_bytes=self._verify_reviewed_bytes,
        )

    def _snapshot_document(self, evidence: PlanReviewEvidence) -> PlanDocument:
        return self.manifests.snapshot_document(evidence)

    def _assemble_requirements_bundle(
        self,
        *,
        project_id: str,
        project_root: Path,
        snapshot: bytes,
        session_id: str | None,
        task_id: str | None,
        plan_path: str,
    ) -> dict[str, object]:
        if task_id is not None:
            task = self.tasks.get_task(task_id, project_id)
            return assemble_requirements_bundle(
                project_root=project_root,
                plan_snapshot=snapshot,
                task_id=task.id,
                task_fields={
                    "title": task.title,
                    "description": task.description,
                    "validation_criteria": task.validation_criteria,
                },
            )
        if session_id is None:  # pragma: no cover - guarded by attempt binding.
            raise RuntimeError("taskless review preparation requires a session")
        variables = self.session_variables.get_variables(session_id)
        anchor = variables.get(REQUEST_ANCHOR_VARIABLE)
        if is_plan_accept_anchor(anchor) and isinstance(anchor, Mapping):
            if not plan_accept_anchor_matches(
                anchor,
                project_root=project_root,
                plan_path=plan_path,
            ):
                raise ReviewEvidenceError(
                    "invalid_request_anchor",
                    "plan-accept anchor targets "
                    f"{anchor.get(ANCHOR_TARGET_FIELD)!r}, not {plan_path!r}; "
                    "run the plan-accept command for this plan to re-seal it",
                )
        return assemble_requirements_bundle(
            project_root=project_root,
            plan_snapshot=snapshot,
            request_anchor=anchor if isinstance(anchor, Mapping) else None,
        )

    def _round_result_for_evidence(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
    ) -> dict[str, object]:
        payload = self.checkpoints.round_result_for_evidence(
            evidence_id,
            round_result,
        )
        if payload["verdict"] not in {"approved", "needs_review"}:
            return payload
        evidence = self.get_evidence(evidence_id)
        raw_findings = payload["findings"]
        if not isinstance(raw_findings, list):
            raise ReviewEvidenceError(
                "invalid_round_result",
                "reviewed round result findings must be an array",
            )
        findings = validate_plan_review_findings(raw_findings, evidence=evidence)
        payload["findings"] = findings
        if payload["verdict"] == "approved":
            validate_approval_condition(
                findings=findings,
                quality_ledger=evidence.quality_ledger or [],
            )
        return payload

    def _changed_sections_since_prior_round(
        self,
        evidence: PlanReviewEvidence,
    ) -> list[str]:
        prior_rows = [
            row
            for row in self.store.list_for_path(
                project_id=evidence.project_id,
                plan_path=evidence.plan_path,
            )
            if row.finalized_at is not None
            and row.expired_at is None
            and row.round_number < evidence.round_number
        ]
        if not prior_rows:
            return []
        prior = prior_rows[-1]
        current_hashes = reviewed_section_hashes(evidence.section_manifest)
        prior_hashes = reviewed_section_hashes(prior.section_manifest)
        return sorted(
            section_id
            for section_id in set(current_hashes) | set(prior_hashes)
            if current_hashes.get(section_id) != prior_hashes.get(section_id)
        )

    def checkpoint_plan_review_lesson_mint(
        self,
        evidence_id: str,
        *,
        status: str,
        detail: Mapping[str, object],
    ) -> PlanReviewEvidence:
        return self.checkpoints.checkpoint_plan_review_lesson_mint(
            evidence_id,
            status=status,
            detail=detail,
        )

    def _resolve_plan_path(
        self,
        project_id: str,
        plan_path: str | Path,
    ) -> tuple[Path, str]:
        return self.manifests.resolve_plan_path(project_id, plan_path)

    def _evidence_path(self, evidence: PlanReviewEvidence) -> Path:
        return self.manifests.evidence_path(evidence)

    def _attempt_is_dead(self, evidence: PlanReviewEvidence) -> bool:
        if not evidence.is_live:
            return False
        if evidence.dispatch_run_id is None:
            expires = evidence.lease_expires_at
            return expires is not None and expires <= datetime.now(UTC)
        run = self.agent_runs.get(evidence.dispatch_run_id)
        return run is None or run.status not in {"pending", "running"}

    @staticmethod
    def _validate_attempt_binding(
        *,
        session_id: str | None,
        task_id: str | None,
        stage: str | None,
    ) -> None:
        interactive = session_id is not None and task_id is None and stage is None
        staged = session_id is None and task_id is not None and stage is not None
        if not (interactive or staged):
            raise ReviewEvidenceError(
                "invalid_attempt_binding",
                "provide session_id, or provide both task_id and stage",
            )

    @staticmethod
    def _matches_attempt(
        evidence: PlanReviewEvidence,
        *,
        round_number: int,
        session_id: str | None,
        task_id: str | None,
        stage: str | None,
    ) -> bool:
        return (
            evidence.round_number == round_number
            and evidence.session_id == session_id
            and evidence.task_id == task_id
            and evidence.stage == stage
        )
