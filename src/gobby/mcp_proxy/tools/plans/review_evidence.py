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
from gobby.mcp_proxy.tools.plans.review_evidence_schemas import (
    CANDIDATE_DISPOSITIONS_SCHEMA,
    INDEX_TOKEN_SCHEMA,
    LANE_RESULTS_SCHEMA,
    LESSON_MINT_DETAIL_SCHEMA,
    PRIOR_FINDING_RESOLUTIONS_SCHEMA,
    REPAIR_ATTESTATIONS_SCHEMA,
    ROUND_RESULT_SCHEMA,
    ROUTING_DECISIONS_SCHEMA,
)
from gobby.plans.consumer_sweep import derive_candidate_site_inventory
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import (
    DEFAULT_SNAPSHOT_PAGE_BYTES,
    MAX_SNAPSHOT_PAGE_BYTES,
    build_inter_round_diff,
    normalize_plan_path,
)
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
            storage = CodeIndexStorage(db)

            def read_last_indexed_at() -> str:
                stats = storage.get_project_stats(project_id)
                return stats.last_indexed_at.isoformat() if stats is not None else ""

            verification = verify_index_token(
                Path(record.repo_path),
                index_token,
                read_last_indexed_at=read_last_indexed_at,
            )
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
        description=(
            "Read-only verification of a settled plan-review index token. "
            "Example: verify a repository_digest, last_indexed_at, and sorted source_files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "index_token": INDEX_TOKEN_SCHEMA,
                "project": {"type": "string"},
            },
            "required": ["index_token"],
            "additionalProperties": False,
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
        description=(
            "Capture immutable, server-hashed evidence for one plan review round. "
            "Example: round 2 carries [{prior_finding_id: F1, decision: repair}] "
            "with its repair attestation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "plan_path": {"type": "string"},
                "round_number": {"type": "integer", "minimum": 1},
                "project": {"type": "string"},
                **_BINDING_PROPERTIES,
                "prior_finding_resolutions": PRIOR_FINDING_RESOLUTIONS_SCHEMA,
                "repair_attestations": REPAIR_ATTESTATIONS_SCHEMA,
            },
            "required": ["plan_path", "round_number"],
            "additionalProperties": False,
        },
        func=prepare_plan_review_round,
    )

    def get_plan_review_snapshot(
        evidence_id: str,
        offset: int = 0,
        limit: int = DEFAULT_SNAPSHOT_PAGE_BYTES,
    ) -> dict[str, object]:
        try:
            return {
                "ok": True,
                **service.snapshot_page(
                    evidence_id,
                    offset=offset,
                    limit=limit,
                ),
            }
        except ReviewEvidenceError as exc:
            return _error_payload(exc, "get_plan_review_snapshot_failed")

    registry.register(
        name="get_plan_review_snapshot",
        description="Page the canonical immutable plan-review evidence envelope.",
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SNAPSHOT_PAGE_BYTES,
                    "default": DEFAULT_SNAPSHOT_PAGE_BYTES,
                },
            },
            "required": ["evidence_id"],
            "additionalProperties": False,
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
        description=(
            "Read-only canonical shadow-manifest derivation for a review snapshot. "
            "Example: {routing_decisions: {5.3: {category: code, tdd: true}}}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "routing_decisions": ROUTING_DECISIONS_SCHEMA,
            },
            "required": ["evidence_id", "routing_decisions"],
            "additionalProperties": False,
        },
        func=derive_plan_review_manifest,
    )

    def validate_plan_review_coverage(
        evidence_id: str,
        lane_results: list[object],
        candidate_dispositions: Mapping[str, object],
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            attestation = service.validate_plan_review_coverage(
                evidence_id,
                lane_results,
                candidate_dispositions,
                routing_decisions,
            )
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        return {"ok": True, "coverage_attestation": attestation}

    registry.register(
        name="validate_plan_review_coverage",
        description=(
            "Read-only validation of review lanes, structured sweep records, "
            "dispositions, and source hashes. Example: three completed lanes plus "
            "a disposition bundle and per-deliverable routing decisions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "lane_results": LANE_RESULTS_SCHEMA,
                "candidate_dispositions": CANDIDATE_DISPOSITIONS_SCHEMA,
                "routing_decisions": ROUTING_DECISIONS_SCHEMA,
            },
            "required": [
                "evidence_id",
                "lane_results",
                "candidate_dispositions",
                "routing_decisions",
            ],
            "additionalProperties": False,
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
        description=(
            "Compare and atomically apply an approved, server-validated M1 manifest. "
            "Example: apply {verdict: approved, findings: [], routing_decisions: {...}, "
            "manifest_entries: [...], coverage_attestation: {...}}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "plan_path": {"type": "string"},
                "run_id": {"type": "string"},
                "round_result": ROUND_RESULT_SCHEMA,
            },
            "required": ["evidence_id", "plan_path", "run_id", "round_result"],
            "additionalProperties": False,
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
        description=(
            "Render the canonical interactive V1 reconciliation checkpoint. "
            "Example: render an approved round_result, or omit it after manifest application."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "round_result": ROUND_RESULT_SCHEMA,
            },
            "required": ["evidence_id"],
            "additionalProperties": False,
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
        description=(
            "Atomically finalize evidence with its canonical durable round result. "
            "Example: finalize {verdict: needs_review, findings: [...], "
            "coverage_attestation: {...}}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "round_result": ROUND_RESULT_SCHEMA,
            },
            "required": ["evidence_id", "round_result"],
            "additionalProperties": False,
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
        description=(
            "Checkpoint the terminal lesson-mint result for an interactive approval. "
            "Example: {status: minted, detail: {minted_lesson_ids: [lesson-1], detail: null}}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["minted", "failed", "none"],
                },
                "detail": LESSON_MINT_DETAIL_SCHEMA,
            },
            "required": ["evidence_id", "status", "detail"],
            "additionalProperties": False,
        },
        func=checkpoint_plan_review_lesson_mint,
    )


def _error_payload(exc: Exception, fallback: str) -> dict[str, Any]:
    if isinstance(exc, ReviewEvidenceError):
        return exc.to_dict()
    return {"ok": False, "error": fallback, "message": str(exc)}
