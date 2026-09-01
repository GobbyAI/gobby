"""Manifest derivation and application for plan-review evidence."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from gobby.plans.digests import canonical_json_sha256
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
    canonical_json_object,
)
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.storage.hub.protocol import HubDatabase, PlanReviewEvidenceMutation
from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
from gobby.storage.projects import LocalProjectManager
from gobby.storage.workspace_machine_scope import require_local_machine_id


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


def _assert_manifest_intent(
    evidence: PlanReviewEvidence,
    *,
    digest: str,
    payload: Mapping[str, object],
) -> dict[str, object] | None:
    """Validate a durable manifest intent and return its idempotent result."""
    if evidence.manifest_digest is not None and evidence.manifest_digest != digest:
        raise ReviewEvidenceError(
            "manifest_payload_conflict",
            "different manifest payload was already recorded for this evidence",
        )
    if evidence.manifest_payload is not None and evidence.manifest_payload != payload:
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
    return None


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
        self._manifest_cache: dict[tuple[str, str], dict[str, object]] = {}

    def derive_plan_review_manifest(
        self,
        evidence_id: str,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        """Derive a canonical shadow manifest without writing the plan."""
        evidence = self.store.require(evidence_id)
        try:
            routing = canonical_json_object(routing_decisions)
        except ReviewEvidenceError as exc:
            return {
                "status": "invalid",
                "routing_decisions": dict(routing_decisions),
                "diagnostics": [{"code": exc.code, "message": str(exc)}],
            }
        routing_digest = canonical_json_sha256({"routing_decisions": routing})
        cache_key = (evidence_id, routing_digest)
        cached = self._manifest_cache.get(cache_key)
        if cached is not None:
            return deepcopy(cached)
        try:
            document = self.snapshot_document(evidence)
            entries = derive_manifest_entries(document, routing)
            with TemporaryDirectory(prefix="gobby-plan-review-") as temp_dir:
                snapshot_path = Path(temp_dir) / Path(evidence.plan_path).name
                snapshot_path.write_bytes(evidence.snapshot)
                render_manifest_plan(snapshot_path, evidence.snapshot, entries)
        except ManifestSynthesisError as exc:
            result: dict[str, object] = {
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
            result = {
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
            result = {
                "status": "invalid",
                "routing_decisions": routing,
                "diagnostics": [
                    {
                        "code": exc.code,
                        "message": str(exc),
                    }
                ],
            }
        else:
            manifest_digest = canonical_json_sha256(entries)
            result = {
                "status": "valid",
                "routing_decisions": routing,
                "manifest_entries": entries,
                "manifest_digest": manifest_digest,
                "entry_count": len(entries),
            }
        self._manifest_cache[cache_key] = deepcopy(result)
        return result

    def apply_plan_review_manifest(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        plan_path: str | Path,
        run_id: str | None,
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
        digest = canonical_json_sha256(payload)
        if not isinstance(entries, list):
            raise ReviewEvidenceError("invalid_manifest", "manifest_entries must be an array")
        resolved = self.evidence_path(evidence)
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        verification_error: ReviewEvidenceError | None = None
        return_result: dict[str, object] | None = None
        with self.db.transaction_immediate(mutation) as transaction:
            evidence = self.store.require(evidence_id, transaction=transaction, for_update=True)
            existing_result = _assert_manifest_intent(
                evidence,
                digest=digest,
                payload=payload,
            )
            if existing_result is not None:
                return existing_result

            try:
                current_bytes = resolved.read_bytes()
            except OSError as exc:
                raise ReviewEvidenceError(
                    "plan_io_error",
                    f"failed to read reviewed plan: {exc}",
                ) from exc
            try:
                verify_reviewed_bytes(evidence, current_bytes)
            except ReviewEvidenceError as error:
                if evidence.manifest_state != "pending":
                    raise
                self.store.revoke_manifest_intent(
                    transaction=transaction,
                    evidence_id=evidence_id,
                )
                verification_error = error
            if verification_error is None:
                rendered = render_manifest_plan(resolved, current_bytes, entries)
                if evidence.manifest_state is None:
                    evidence = self.store.begin_manifest_apply(
                        transaction=transaction,
                        evidence_id=evidence_id,
                        digest=digest,
                        payload=payload,
                    )
                existing_result = _assert_manifest_intent(
                    evidence,
                    digest=digest,
                    payload=payload,
                )
                if existing_result is not None:
                    return existing_result
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
                completed = self.store.complete_manifest_apply(
                    transaction=transaction,
                    evidence_id=evidence_id,
                    result=result,
                )
                return_result = completed.manifest_result or result

        if verification_error is not None:
            raise verification_error
        if return_result is None:
            raise RuntimeError("manifest apply completed without a result")
        return return_result

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
        if project is None:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {project_id}",
            )
        try:
            machine_id = require_local_machine_id(
                None, resource_kind="project_checkout", resource_id=project_id
            )
            root = Path(require_root(self.db, project_id, machine_id)).resolve(strict=True)
        except CheckoutNotFoundError as exc:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {project_id}",
            ) from exc
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
