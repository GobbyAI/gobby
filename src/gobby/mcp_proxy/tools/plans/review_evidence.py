"""MCP registration for durable plan-review evidence operations."""

from __future__ import annotations

import subprocess  # nosec B404 - fixed local gcode argv.
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from gobby.agents.code_index import (
    IndexInventoryError,
    IndexToken,
    settle_indexed_value,
    verify_index_token,
)
from gobby.code_index.storage import CodeIndexStorage
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.plans.consumer_sweep import derive_candidate_site_inventory
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import build_inter_round_diff, normalize_plan_path
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.review_findings import validate_plan_review_findings
from gobby.plans.review_repair import RepairUniverse, derive_repair_universe
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.utils.native_bin import resolve_native_bin_or_default

_BINDING_PROPERTIES: dict[str, dict[str, object]] = {
    "session_id": {"type": "string"},
    "task_id": {"type": "string"},
    "stage": {"type": "string"},
}


@dataclass(frozen=True)
class _RepairUniverseCodeIndex:
    storage: CodeIndexStorage


def _derive_settled_repair_universe(
    *,
    db: HubDatabase,
    project_id: str,
    project_root: Path,
    prior_evidence_id: str,
    plan_path: str,
    repair_finding_ids: list[str],
) -> tuple[IndexToken, RepairUniverse]:
    service = PlanReviewEvidenceService(db)
    prior_evidence = service.get_evidence(prior_evidence_id)
    if prior_evidence.project_id != project_id:
        raise ReviewEvidenceError(
            "repair_universe_project_mismatch",
            "prior evidence belongs to a different project",
        )
    if prior_evidence.finalized_at is None or prior_evidence.round_result is None:
        raise ReviewEvidenceError(
            "repair_universe_prior_unfinalized",
            "repair universe requires finalized prior evidence",
        )
    raw_findings = prior_evidence.round_result.get("findings")
    if not isinstance(raw_findings, list) or any(
        not isinstance(finding, Mapping) for finding in raw_findings
    ):
        raise ReviewEvidenceError(
            "invalid_repair_attestation",
            "prior round_result.findings must be an array of objects",
        )
    findings = validate_plan_review_findings(
        cast(list[Mapping[str, object]], raw_findings),
        evidence=prior_evidence,
    )
    resolved_plan_path = normalize_plan_path(project_root, plan_path)
    storage = CodeIndexStorage(db)
    code_index = _RepairUniverseCodeIndex(storage)

    def read_last_indexed_at() -> str:
        stats = storage.get_project_stats(project_id)
        return stats.last_indexed_at.isoformat() if stats is not None else ""

    def derive() -> RepairUniverse:
        current_snapshot = resolved_plan_path.read_bytes()
        inventory = derive_candidate_site_inventory(
            diff=build_inter_round_diff(prior_evidence.snapshot, current_snapshot),
            project_id=project_id,
            code_index=code_index,
        )
        return derive_repair_universe(
            prior_findings=findings,
            inventory=inventory,
            repair_finding_ids=repair_finding_ids,
        )

    return settle_indexed_value(
        project_root,
        index_operation=lambda: _index_repository(project_root),
        read_last_indexed_at=read_last_indexed_at,
        derive=derive,
    )


def _index_repository(project_root: Path) -> None:
    binary = resolve_native_bin_or_default("gcode")
    try:
        completed = subprocess.run(  # nosec B603 - fixed argv plus trusted local path.
            [binary, "index", "--quiet", "--project", str(project_root)],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"gcode index failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"gcode index failed: {detail[:500]}")


def register_review_evidence_tools(
    registry: InternalToolRegistry,
    db: HubDatabase,
    *,
    resolve_project_id: Callable[[str | None], str],
) -> None:
    """Register the trusted evidence producer and its lifecycle operations."""
    service = PlanReviewEvidenceService(db)
    projects = LocalProjectManager(db)

    def verify_plan_review_index_token(
        index_token: Mapping[str, object],
        project: str | None = None,
    ) -> dict[str, object]:
        project_id = resolve_project_id(project)
        record = projects.get(project_id)
        if record is None or record.repo_path is None:
            return IndexInventoryError(
                "inventory_unavailable",
                f"project has no local repository: {project_id}",
            ).to_dict()
        try:
            verification = verify_index_token(Path(record.repo_path), index_token)
        except (IndexInventoryError, OSError) as exc:
            if isinstance(exc, IndexInventoryError):
                return exc.to_dict()
            return IndexInventoryError(
                "inventory_unavailable",
                f"index token verification failed: {exc}",
            ).to_dict()
        return {"ok": True, "verification": verification.to_dict()}

    registry.register(
        name="verify_plan_review_index_token",
        description="Read-only verification of a settled plan-review index token.",
        input_schema={
            "type": "object",
            "properties": {
                "index_token": {"type": "object"},
                "project": {"type": "string"},
            },
            "required": ["index_token"],
        },
        func=verify_plan_review_index_token,
    )

    def derive_plan_review_repair_universe(
        prior_evidence_id: str,
        plan_path: str,
        repair_finding_ids: list[str],
        project: str | None = None,
    ) -> dict[str, object]:
        try:
            project_id = resolve_project_id(project)
            record = projects.get(project_id)
            if record is None or record.repo_path is None:
                raise IndexInventoryError(
                    "inventory_unavailable",
                    f"project has no local repository: {project_id}",
                )
            token, universe = _derive_settled_repair_universe(
                db=db,
                project_id=project_id,
                project_root=Path(record.repo_path),
                prior_evidence_id=prior_evidence_id,
                plan_path=plan_path,
                repair_finding_ids=repair_finding_ids,
            )
        except IndexInventoryError as exc:
            return exc.to_dict()
        except (ReviewEvidenceError, OSError, RuntimeError, ValueError) as exc:
            return _error_payload(exc, "repair_universe_unavailable")
        return {
            "ok": True,
            "repair_universe": universe.to_dict(),
            "repair_universe_digest": universe.digest,
            "index_token": token.to_dict(),
        }

    registry.register(
        name="derive_plan_review_repair_universe",
        description=(
            "Derive the settled read-only repair site graph and canonical digest "
            "before attestation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prior_evidence_id": {"type": "string"},
                "plan_path": {"type": "string"},
                "repair_finding_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "project": {"type": "string"},
            },
            "required": ["prior_evidence_id", "plan_path", "repair_finding_ids"],
        },
        func=derive_plan_review_repair_universe,
    )

    def prepare_plan_review_round(
        plan_path: str,
        round_number: int,
        project: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        stage: str | None = None,
        prior_finding_resolutions: list[dict[str, object]] | None = None,
        repair_attestations: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        try:
            prepared = service.prepare_plan_review_round(
                project_id=resolve_project_id(project),
                plan_path=plan_path,
                round_number=round_number,
                session_id=session_id,
                task_id=task_id,
                stage=stage,
                prior_finding_resolutions=prior_finding_resolutions,
                repair_attestations=repair_attestations,
            )
        except (ReviewEvidenceError, ValueError, OSError) as exc:
            return _error_payload(exc, "prepare_plan_review_round_failed")
        return {"ok": True, **prepared.to_dict()}

    registry.register(
        name="prepare_plan_review_round",
        description="Capture immutable, server-hashed evidence for one plan review round.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_path": {"type": "string"},
                "round_number": {"type": "integer", "minimum": 1},
                "project": {"type": "string"},
                **_BINDING_PROPERTIES,
            },
            "required": ["plan_path", "round_number"],
        },
        func=prepare_plan_review_round,
    )

    def get_plan_review_snapshot(evidence_id: str) -> dict[str, object]:
        try:
            payload = service.snapshot_payload(evidence_id)
            snapshot = payload.pop("snapshot")
            if not isinstance(snapshot, bytes):
                raise ReviewEvidenceError(
                    "invalid_evidence_row",
                    "stored plan snapshot is not bytes",
                )
            return {
                "ok": True,
                **payload,
                "snapshot": snapshot.decode("utf-8"),
            }
        except (ReviewEvidenceError, UnicodeDecodeError) as exc:
            return _error_payload(exc, "get_plan_review_snapshot_failed")

    registry.register(
        name="get_plan_review_snapshot",
        description="Return the immutable UTF-8 snapshot reviewed by the adversary.",
        input_schema={
            "type": "object",
            "properties": {"evidence_id": {"type": "string"}},
            "required": ["evidence_id"],
        },
        func=get_plan_review_snapshot,
    )

    def bind_evidence_run(evidence_id: str, run_id: str) -> dict[str, object]:
        try:
            evidence = service.bind_evidence_run(evidence_id, run_id)
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {
            "ok": True,
            "evidence_id": evidence.evidence_id,
            "run_id": evidence.dispatch_run_id,
            "lease_expires_at": None,
        }

    registry.register(
        name="bind_evidence_run",
        description="Attach a spawned agent run to prepared evidence exactly once.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["evidence_id", "run_id"],
        },
        func=bind_evidence_run,
    )

    def expire_plan_review_evidence(
        evidence_id: str,
        spawn_failed: bool = False,
    ) -> dict[str, object]:
        try:
            evidence = service.expire_plan_review_evidence(
                evidence_id,
                spawn_failed=spawn_failed,
            )
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {
            "ok": True,
            "evidence_id": evidence.evidence_id,
            "expired_at": evidence.expired_at.isoformat() if evidence.expired_at else None,
        }

    registry.register(
        name="expire_plan_review_evidence",
        description="Expire evidence after spawn/bind failure or a provably dead attempt.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "spawn_failed": {"type": "boolean", "default": False},
            },
            "required": ["evidence_id"],
        },
        func=expire_plan_review_evidence,
    )

    def verify_plan_unchanged(evidence_id: str, plan_path: str) -> dict[str, object]:
        try:
            service.verify_plan_unchanged(evidence_id, plan_path)
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {"ok": True, "evidence_id": evidence_id, "fresh": True}

    registry.register(
        name="verify_plan_unchanged",
        description="Compare reviewed plan sections with the immutable evidence manifest.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "plan_path": {"type": "string"},
            },
            "required": ["evidence_id", "plan_path"],
        },
        func=verify_plan_unchanged,
    )

    def derive_plan_review_manifest(
        evidence_id: str,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            result = service.derive_plan_review_manifest(evidence_id, routing_decisions)
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {"ok": True, **result}

    registry.register(
        name="derive_plan_review_manifest",
        description="Read-only canonical shadow-manifest derivation for a review snapshot.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "routing_decisions": {"type": "object"},
            },
            "required": ["evidence_id", "routing_decisions"],
        },
        func=derive_plan_review_manifest,
    )

    def validate_plan_review_coverage(
        evidence_id: str,
        lane_results: list[object],
        candidate_dispositions: Mapping[str, object],
        shadow_manifest_status: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            attestation = service.validate_plan_review_coverage(
                evidence_id,
                lane_results,
                candidate_dispositions,
                shadow_manifest_status,
            )
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {"ok": True, "coverage_attestation": attestation}

    registry.register(
        name="validate_plan_review_coverage",
        description=(
            "Read-only validation of review lanes, structured sweep records, "
            "dispositions, and source hashes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "lane_results": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "candidate_dispositions": {
                    "type": "object",
                    "properties": {
                        "cross_lane_interactions": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                        "adjacent_variant_sweeps": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                        "causal_repair_sweeps": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                        "candidate_dispositions": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                    "required": [
                        "cross_lane_interactions",
                        "adjacent_variant_sweeps",
                        "causal_repair_sweeps",
                        "candidate_dispositions",
                    ],
                    "additionalProperties": False,
                },
                "shadow_manifest_status": {"type": "object"},
            },
            "required": [
                "evidence_id",
                "lane_results",
                "candidate_dispositions",
                "shadow_manifest_status",
            ],
        },
        func=validate_plan_review_coverage,
    )

    def apply_plan_review_manifest(
        evidence_id: str,
        plan_path: str,
        run_id: str,
        round_result: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            result = service.apply_plan_review_manifest(
                evidence_id,
                round_result,
                plan_path=plan_path,
                run_id=run_id,
            )
        except (ReviewEvidenceError, OSError) as exc:
            return _error_payload(exc, "apply_plan_review_manifest_failed")
        return {"ok": True, "result": result}

    registry.register(
        name="apply_plan_review_manifest",
        description="Compare and atomically apply an approved, server-validated M1 manifest.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "plan_path": {"type": "string"},
                "run_id": {"type": "string"},
                "round_result": {"type": "object"},
            },
            "required": ["evidence_id", "plan_path", "run_id", "round_result"],
        },
        func=apply_plan_review_manifest,
    )

    def render_v1_round_checkpoint(
        evidence_id: str,
        round_result: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            checkpoint = service.render_v1_round_checkpoint(evidence_id, round_result)
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {
            "ok": True,
            "evidence_id": evidence_id,
            "checkpoint": checkpoint.decode("utf-8"),
        }

    registry.register(
        name="render_v1_round_checkpoint",
        description="Render the canonical interactive V1 reconciliation checkpoint.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "round_result": {"type": "object"},
            },
            "required": ["evidence_id"],
        },
        func=render_v1_round_checkpoint,
    )

    def finalize_plan_review_evidence(
        evidence_id: str,
        round_result: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            evidence = service.finalize_plan_review_evidence(evidence_id, round_result)
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {
            "ok": True,
            "evidence_id": evidence.evidence_id,
            "round_result": evidence.round_result,
            "lesson_mint_status": evidence.lesson_mint_status,
        }

    registry.register(
        name="finalize_plan_review_evidence",
        description="Atomically finalize evidence with its canonical durable round result.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "round_result": {"type": "object"},
            },
            "required": ["evidence_id", "round_result"],
        },
        func=finalize_plan_review_evidence,
    )

    def checkpoint_plan_review_lesson_mint(
        evidence_id: str,
        status: str,
        detail: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            evidence = service.checkpoint_plan_review_lesson_mint(
                evidence_id,
                status=status,
                detail=detail,
            )
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {
            "ok": True,
            "evidence_id": evidence.evidence_id,
            "lesson_mint_status": evidence.lesson_mint_status,
            "lesson_mint_detail": evidence.lesson_mint_detail,
        }

    registry.register(
        name="checkpoint_plan_review_lesson_mint",
        description="Checkpoint the terminal lesson-mint result for an interactive approval.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["minted", "failed", "none"],
                },
                "detail": {"type": "object"},
            },
            "required": ["evidence_id", "status", "detail"],
        },
        func=checkpoint_plan_review_lesson_mint,
    )


def _error_payload(exc: Exception, fallback: str) -> dict[str, Any]:
    if isinstance(exc, ReviewEvidenceError):
        return exc.to_dict()
    return {"ok": False, "error": fallback, "message": str(exc)}
