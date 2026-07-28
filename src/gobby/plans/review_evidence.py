"""Durable preparation and lifecycle service for plan-review evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from gobby.plans.manifest_emitter import ManifestSynthesisError, derive_manifest_entries
from gobby.plans.parser import PlanDocument, PlanParseError, parse_plan
from gobby.plans.review_coverage import review_complexity, validate_review_coverage
from gobby.plans.review_evidence_io import (
    atomic_write_bytes,
    build_section_manifest,
    ensure_checkpoint,
    normalize_plan_path,
    parse_checkpoints,
    render_checkpoint,
    render_manifest_plan,
    reviewed_section_hashes,
)
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    PreparedReviewEvidence,
    ReviewEvidenceError,
    canonical_json_bytes,
    validate_round_result,
)
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.plans.review_ledger import merge_quality_ledger
from gobby.plans.review_repair import repair_preparation_for_round
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase, PlanReviewEvidenceMutation, Transaction
from gobby.storage.projects import LocalProjectManager

EVIDENCE_LEASE_SECONDS = 7_200


class PlanReviewEvidenceService:
    """Coordinate immutable snapshots with durable evidence lifecycle state."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self.store = PlanReviewEvidenceStore(db)
        self.projects = LocalProjectManager(db)
        self.agent_runs = LocalAgentRunManager(db)

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
            self._reconcile_checkpoints(
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
            if active is not None and active.is_interactive and active.round_result is not None:
                self._drain_interactive_intent(
                    transaction=transaction,
                    evidence=active,
                    plan_path=resolved,
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
                        context = repair_preparation_for_round(
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
                        )
                        if context is not None:
                            active = self.store.write_preparation_context(
                                transaction=transaction,
                                evidence_id=active.evidence_id,
                                repair_attestations=context.repair_attestations,
                                prior_round_context=context.prior_round_context,
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
                    context = repair_preparation_for_round(
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
                    if context is not None:
                        evidence = self.store.write_preparation_context(
                            transaction=transaction,
                            evidence_id=evidence.evidence_id,
                            repair_attestations=context.repair_attestations,
                            prior_round_context=context.prior_round_context,
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

    def derive_plan_review_manifest(
        self,
        evidence_id: str,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        """Derive a canonical shadow manifest without writing the plan."""
        evidence = self.get_evidence(evidence_id)
        routing = dict(routing_decisions)
        try:
            document = self._snapshot_document(evidence)
            entries = derive_manifest_entries(document, routing)
            with TemporaryDirectory(prefix="gobby-plan-review-") as temp_dir:
                snapshot_path = Path(temp_dir) / Path(evidence.plan_path).name
                snapshot_path.write_bytes(evidence.snapshot)
                render_manifest_plan(snapshot_path, evidence.snapshot, entries)
        except ManifestSynthesisError as exc:
            return {
                "status": "invalid",
                "routing_decisions": routing,
                "diagnostics": [
                    {
                        "code": "invalid_routing_decisions",
                        "message": str(exc),
                    }
                ],
            }
        except PlanParseError as exc:
            return {
                "status": "invalid",
                "routing_decisions": routing,
                "diagnostics": [
                    {
                        "code": "invalid_plan_snapshot",
                        "line": line,
                        "message": message,
                    }
                    for line, message in exc.errors
                ],
            }
        except ReviewEvidenceError as exc:
            return {
                "status": "invalid",
                "routing_decisions": routing,
                "diagnostics": [
                    {
                        "code": exc.code,
                        "message": str(exc),
                    }
                ],
            }
        manifest_digest = hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "status": "valid",
            "routing_decisions": routing,
            "manifest_entries": entries,
            "manifest_digest": manifest_digest,
            "entry_count": len(entries),
        }

    def validate_plan_review_coverage(
        self,
        evidence_id: str,
        lane_results: list[object],
        candidate_dispositions: Mapping[str, object],
        shadow_manifest_status: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate all research lanes and return a canonical coverage attestation."""
        evidence = self.get_evidence(evidence_id)
        shadow = dict(shadow_manifest_status)
        routing = shadow.get("routing_decisions")
        if not isinstance(routing, Mapping):
            raise ReviewEvidenceError(
                "invalid_shadow_manifest",
                "shadow_manifest_status.routing_decisions must be an object",
            )
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
            shadow_manifest_status=shadow,
            expected_shadow_manifest_status=expected_shadow,
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
        evidence = self.get_evidence(evidence_id)
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
            payload = self._round_result_for_evidence(evidence_id, round_result)
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

    def finalize_plan_review_evidence(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        payload = self._round_result_for_evidence(evidence_id, round_result)
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
            plan_path = self._evidence_path(evidence)
            matching = [
                checkpoint
                for checkpoint in parse_checkpoints(plan_path.read_bytes())
                if checkpoint["evidence_id"] == evidence_id
            ]
            expected = self.render_v1_round_checkpoint(evidence_id, payload)
            if not matching or expected not in plan_path.read_bytes():
                raise ReviewEvidenceError(
                    "missing_v1_checkpoint",
                    "interactive finalization requires the durable V1 checkpoint",
                )
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            prior_rows = [
                row
                for row in self.store.list_for_path(
                    project_id=evidence.project_id,
                    plan_path=evidence.plan_path,
                    transaction=transaction,
                    for_update=True,
                )
                if row.finalized_at is not None
                and row.expired_at is None
                and row.round_number < evidence.round_number
            ]
            quality_ledger = merge_quality_ledger(
                prior_ledger=(prior_rows[-1].quality_ledger or []) if prior_rows else [],
                round_number=evidence.round_number,
                current_section_hashes=reviewed_section_hashes(evidence.section_manifest),
                round_result=payload,
                prior_round_context=evidence.prior_round_context,
            )
            self.store.write_quality_ledger(
                transaction=transaction,
                evidence_id=evidence_id,
                quality_ledger=quality_ledger,
            )
            return self.store.finalize(
                transaction=transaction,
                evidence_id=evidence_id,
                round_result=payload,
                approval=payload["verdict"] == "approved",
            )

    def apply_plan_review_manifest(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        plan_path: str | Path,
        run_id: str,
    ) -> dict[str, object]:
        evidence = self.get_evidence(evidence_id)
        payload = self._round_result_for_evidence(evidence_id, round_result)
        if payload["verdict"] != "approved":
            raise ReviewEvidenceError(
                "invalid_manifest",
                "manifest application requires an approved round result",
            )
        _, relative_path = self._resolve_plan_path(evidence.project_id, plan_path)
        if relative_path != evidence.plan_path:
            raise ReviewEvidenceError("wrong_plan", "evidence belongs to another plan")
        self.authorize_current_attempt(
            evidence_id,
            project_id=evidence.project_id,
            plan_path=plan_path,
            round_number=evidence.round_number,
            session_id=evidence.session_id,
            task_id=evidence.task_id,
            stage=evidence.stage,
            run_id=run_id,
        )
        routing = payload.get("routing_decisions")
        if not isinstance(routing, Mapping):
            raise ReviewEvidenceError(
                "invalid_manifest",
                "approved round result requires routing_decisions",
            )
        canonical_manifest = self.derive_plan_review_manifest(evidence_id, routing)
        if canonical_manifest["status"] != "valid":
            raise ReviewEvidenceError(
                "invalid_manifest",
                "canonical manifest derivation failed",
                details={"diagnostics": canonical_manifest.get("diagnostics", [])},
            )
        entries = canonical_manifest["manifest_entries"]
        if payload.get("manifest_entries") != entries:
            raise ReviewEvidenceError(
                "noncanonical_manifest",
                "round_result.manifest_entries differs from canonical derivation",
            )
        attestation = payload["coverage_attestation"]
        if not isinstance(attestation, dict):  # validated above; narrows for mypy.
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "coverage_attestation must be an object",
            )
        shadow_status = attestation["shadow_manifest_status"]
        if not isinstance(shadow_status, dict):  # validated above; narrows for mypy.
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "shadow_manifest_status must be an object",
            )
        if (
            shadow_status.get("manifest_digest") != canonical_manifest["manifest_digest"]
            or shadow_status.get("entry_count") != canonical_manifest["entry_count"]
        ):
            raise ReviewEvidenceError(
                "shadow_manifest_mismatch",
                "coverage attestation does not bind the canonical manifest",
            )
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if not isinstance(entries, list):  # validated above; narrows for mypy.
            raise ReviewEvidenceError("invalid_manifest", "manifest_entries must be an array")
        resolved = self._evidence_path(evidence)
        if evidence.manifest_digest is not None and evidence.manifest_digest != digest:
            raise ReviewEvidenceError(
                "manifest_payload_conflict",
                "different manifest payload was already recorded for this evidence",
            )
        if evidence.manifest_state == "revoked":
            raise ReviewEvidenceError("manifest_revoked", "manifest intent was revoked")
        if evidence.manifest_state == "applied":
            if evidence.manifest_result is None:
                raise ReviewEvidenceError(
                    "invalid_evidence_row",
                    "applied manifest evidence has no result",
                )
            return evidence.manifest_result
        current_bytes = resolved.read_bytes()
        try:
            self._verify_reviewed_bytes(evidence, current_bytes)
        except ReviewEvidenceError:
            if evidence.manifest_state == "pending":
                mutation = PlanReviewEvidenceMutation(
                    project_id=evidence.project_id,
                    plan_path=evidence.plan_path,
                )
                with self.db.transaction_immediate(mutation) as transaction:
                    self.store.revoke_manifest_intent(
                        transaction=transaction,
                        evidence_id=evidence_id,
                    )
            raise
        rendered = render_manifest_plan(resolved, current_bytes, entries)
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        if evidence.manifest_state is None:
            with self.db.transaction_immediate(mutation) as transaction:
                evidence = self.store.begin_manifest_apply(
                    transaction=transaction,
                    evidence_id=evidence_id,
                    digest=digest,
                    payload=payload,
                )
        if evidence.manifest_digest != digest or evidence.manifest_payload != payload:
            raise ReviewEvidenceError(
                "manifest_payload_conflict",
                "different manifest payload was already recorded for this evidence",
            )
        if evidence.manifest_state == "revoked":
            raise ReviewEvidenceError("manifest_revoked", "manifest intent was revoked")
        if evidence.manifest_state == "applied":
            if evidence.manifest_result is None:
                raise ReviewEvidenceError(
                    "invalid_evidence_row",
                    "applied manifest evidence has no result",
                )
            return evidence.manifest_result
        if evidence.manifest_state != "pending":
            raise ReviewEvidenceError(
                "invalid_evidence_row",
                "manifest evidence did not enter the pending state",
            )
        atomic_write_bytes(resolved, rendered)
        result: dict[str, object] = {
            "evidence_id": evidence_id,
            "manifest_digest": digest,
            "applied": True,
        }
        with self.db.transaction_immediate(mutation) as transaction:
            completed = self.store.complete_manifest_apply(
                transaction=transaction,
                evidence_id=evidence_id,
                result=result,
            )
        return completed.manifest_result or result

    def _snapshot_document(self, evidence: PlanReviewEvidence) -> PlanDocument:
        with TemporaryDirectory(prefix="gobby-plan-review-") as temp_dir:
            snapshot_path = Path(temp_dir) / Path(evidence.plan_path).name
            snapshot_path.write_bytes(evidence.snapshot)
            return parse_plan(snapshot_path, parse_mode="draft")

    @staticmethod
    def _round_result_for_evidence(
        evidence_id: str,
        round_result: Mapping[str, object],
    ) -> dict[str, object]:
        payload = validate_round_result(round_result)
        coverage = payload["coverage_attestation"]
        if not isinstance(coverage, dict) or coverage.get("evidence_id") != evidence_id:
            raise ReviewEvidenceError(
                "coverage_evidence_mismatch",
                "coverage attestation belongs to a different review evidence snapshot",
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
        if status not in {"minted", "failed", "none"}:
            raise ReviewEvidenceError(
                "invalid_lesson_mint_status",
                "status must be minted, failed, or none",
            )
        evidence = self.get_evidence(evidence_id)
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

    def _resolve_plan_path(
        self,
        project_id: str,
        plan_path: str | Path,
    ) -> tuple[Path, str]:
        project = self.projects.get(project_id)
        if project is None or project.repo_path is None:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {project_id}",
            )
        root = Path(project.repo_path).resolve(strict=True)
        resolved = normalize_plan_path(root, plan_path)
        return resolved, resolved.relative_to(root).as_posix()

    def _evidence_path(self, evidence: PlanReviewEvidence) -> Path:
        resolved, relative_path = self._resolve_plan_path(
            evidence.project_id,
            evidence.plan_path,
        )
        if relative_path != evidence.plan_path:
            raise ReviewEvidenceError("wrong_plan", "evidence plan path changed")
        return resolved

    def _reconcile_checkpoints(
        self,
        *,
        transaction: Transaction,
        project_id: str,
        plan_path: str,
        session_id: str | None,
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
            lineage_matches = (
                evidence.project_id == project_id
                and evidence.plan_path == plan_path
                and evidence.session_id == session_id
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
                self.store.finalize(
                    transaction=transaction,
                    evidence_id=evidence_id,
                    round_result=payload,
                    approval=payload.get("verdict") == "approved",
                )

    def _drain_interactive_intent(
        self,
        *,
        transaction: Transaction,
        evidence: PlanReviewEvidence,
        plan_path: Path,
    ) -> None:
        round_result = evidence.round_result
        if round_result is None:
            return
        if evidence.manifest_state == "pending":
            try:
                self.verify_plan_unchanged(evidence.evidence_id, plan_path)
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
        checkpoint = self.render_v1_round_checkpoint(evidence.evidence_id)
        ensure_checkpoint(plan_path, checkpoint)
        self.store.finalize(
            transaction=transaction,
            evidence_id=evidence.evidence_id,
            round_result=round_result,
            approval=True,
        )

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
