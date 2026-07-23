"""MCP registration for durable plan-review evidence operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.hub.protocol import HubDatabase

_BINDING_PROPERTIES: dict[str, dict[str, object]] = {
    "session_id": {"type": "string"},
    "task_id": {"type": "string"},
    "stage": {"type": "string"},
}


def register_review_evidence_tools(
    registry: InternalToolRegistry,
    db: HubDatabase,
    *,
    resolve_project_id: Callable[[str | None], str],
) -> None:
    """Register the trusted evidence producer and its lifecycle operations."""
    service = PlanReviewEvidenceService(db)

    def prepare_plan_review_round(
        plan_path: str,
        round_number: int,
        project: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        stage: str | None = None,
    ) -> dict[str, object]:
        try:
            prepared = service.prepare_plan_review_round(
                project_id=resolve_project_id(project),
                plan_path=plan_path,
                round_number=round_number,
                session_id=session_id,
                task_id=task_id,
                stage=stage,
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
