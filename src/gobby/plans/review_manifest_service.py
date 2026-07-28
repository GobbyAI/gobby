"""Manifest derivation and application for plan-review evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from gobby.plans.manifest_emitter import ManifestSynthesisError, derive_manifest_entries
from gobby.plans.parser import PlanDocument, PlanParseError, parse_plan
from gobby.plans.review_evidence_io import (
    atomic_write_bytes,
    normalize_plan_path,
    render_manifest_plan,
)
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    canonical_json_bytes,
)
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.storage.hub.protocol import HubDatabase, PlanReviewEvidenceMutation
from gobby.storage.projects import LocalProjectManager


class RoundResultResolver(Protocol):
    """Validate and bind a round result to an evidence row."""

    def __call__(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
    ) -> dict[str, object]: ...


class AttemptAuthorizer(Protocol):
    """Authorize an evidence row for its bound agent run."""

    def __call__(
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
    ) -> PlanReviewEvidence: ...


class ReviewedBytesVerifier(Protocol):
    """Verify that reviewed plan sections still match their snapshot."""

    def __call__(
        self,
        evidence: PlanReviewEvidence,
        current_bytes: bytes,
    ) -> None: ...


class ReviewManifestService:
    """Own canonical manifest derivation and durable apply orchestration."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        store: PlanReviewEvidenceStore,
        projects: LocalProjectManager,
    ) -> None:
        self.db = db
        self.store = store
        self.projects = projects

    def derive_plan_review_manifest(
        self,
        evidence_id: str,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        """Derive a canonical shadow manifest without writing the plan."""
        evidence = self.store.require(evidence_id)
        routing = dict(routing_decisions)
        try:
            document = self.snapshot_document(evidence)
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

    def apply_plan_review_manifest(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        plan_path: str | Path,
        run_id: str,
        resolve_round_result: RoundResultResolver,
        authorize_attempt: AttemptAuthorizer,
        verify_reviewed_bytes: ReviewedBytesVerifier,
    ) -> dict[str, object]:
        evidence = self.store.require(evidence_id)
        payload = resolve_round_result(evidence_id, round_result)
        if payload["verdict"] != "approved":
            raise ReviewEvidenceError(
                "invalid_manifest",
                "manifest application requires an approved round result",
            )
        _, relative_path = self.resolve_plan_path(evidence.project_id, plan_path)
        if relative_path != evidence.plan_path:
            raise ReviewEvidenceError("wrong_plan", "evidence belongs to another plan")
        authorize_attempt(
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
        if not isinstance(attestation, dict):
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "coverage_attestation must be an object",
            )
        shadow_status = attestation["shadow_manifest_status"]
        if not isinstance(shadow_status, dict):
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
        if not isinstance(entries, list):
            raise ReviewEvidenceError("invalid_manifest", "manifest_entries must be an array")
        resolved = self.evidence_path(evidence)
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
            verify_reviewed_bytes(evidence, current_bytes)
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

    @staticmethod
    def snapshot_document(evidence: PlanReviewEvidence) -> PlanDocument:
        with TemporaryDirectory(prefix="gobby-plan-review-") as temp_dir:
            snapshot_path = Path(temp_dir) / Path(evidence.plan_path).name
            snapshot_path.write_bytes(evidence.snapshot)
            return parse_plan(snapshot_path, parse_mode="draft")

    def resolve_plan_path(
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

    def evidence_path(self, evidence: PlanReviewEvidence) -> Path:
        resolved, relative_path = self.resolve_plan_path(
            evidence.project_id,
            evidence.plan_path,
        )
        if relative_path != evidence.plan_path:
            raise ReviewEvidenceError("wrong_plan", "evidence plan path changed")
        return resolved
