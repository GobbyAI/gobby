"""MCP tools for durable interactive plan-round vote artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.vote_artifacts import (
    INTERACTION_TOOLS,
    PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
    ROUND_KINDS,
    VOTE_DECISIONS,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import get_current_agent_run_id, get_current_session_id
from gobby.workflows.state_manager import SessionVariableManager

PRESENTED_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding_id": {"type": "string", "minLength": 1},
        "target_section_id": {"type": "string", "minLength": 1},
        "full_item_text": {"type": "string", "minLength": 1},
        "proposed_edit_text": {"type": "string", "minLength": 1},
    },
    "required": [
        "finding_id",
        "target_section_id",
        "full_item_text",
        "proposed_edit_text",
    ],
    "additionalProperties": False,
}

VOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vote_id": {"type": "string", "minLength": 1},
        "finding_id": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "enum": sorted(VOTE_DECISIONS)},
    },
    "required": ["vote_id", "finding_id", "decision"],
    "additionalProperties": False,
}


def register_plan_vote_artifact_tools(
    registry: InternalToolRegistry,
    db: HubDatabase,
    *,
    resolve_project_id: Callable[[str | None], str],
) -> None:
    """Register durable plan vote artifact write/read operations."""
    sessions = SessionManager(db)
    variables = SessionVariableManager(db)
    evidence_service = PlanReviewEvidenceService(db)

    def record_plan_vote_artifact(
        evidence_id: str,
        plan_path: str,
        round_kind: str,
        round_number: int,
        interaction_tool: str,
        interaction_payload: Mapping[str, object],
        votes: list[Mapping[str, object]],
        project: str | None = None,
    ) -> dict[str, object]:
        session_ref = get_current_session_id()
        if session_ref is None:
            return {
                "ok": False,
                "error": "session_required",
                "message": "record_plan_vote_artifact requires an active session",
            }
        project_id = resolve_project_id(project)
        try:
            session_id = sessions.resolve_session_reference(
                session_ref,
                project_id=project_id,
            )
            receipt = variables.get_variables(session_id).get(
                PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE
            )
            evidence = evidence_service.record_observed_vote_artifact(
                evidence_id=evidence_id,
                caller_session_id=session_id,
                plan_path=plan_path,
                round_kind=round_kind,
                round_number=round_number,
                interaction_tool=interaction_tool,
                interaction_payload=interaction_payload,
                votes=votes,
                receipt=receipt,
            )
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        except ValueError as exc:
            return {"ok": False, "error": "invalid_session", "message": str(exc)}
        return {"ok": True, "artifact": evidence.vote_artifact}

    registry.register(
        name="record_plan_vote_artifact",
        description=(
            "Validate and durably record one interactive enhancement/adversary vote round. "
            "The interaction payload must contain every item's full text and proposed edit; "
            "votes must name one decision per finding. Free-text-only and blanket decisions "
            "are rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "minLength": 1},
                "plan_path": {"type": "string", "minLength": 1},
                "round_kind": {"type": "string", "enum": sorted(ROUND_KINDS)},
                "round_number": {"type": "integer", "minimum": 1},
                "interaction_tool": {
                    "type": "string",
                    "enum": sorted(INTERACTION_TOOLS),
                },
                "interaction_payload": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": PRESENTED_ITEM_SCHEMA,
                        }
                    },
                    "required": ["items"],
                    "additionalProperties": True,
                },
                "votes": {
                    "type": "array",
                    "items": VOTE_SCHEMA,
                },
                "project": {"type": "string"},
            },
            "required": [
                "evidence_id",
                "plan_path",
                "round_kind",
                "round_number",
                "interaction_tool",
                "interaction_payload",
                "votes",
            ],
            "additionalProperties": False,
        },
        func=record_plan_vote_artifact,
    )

    def coordinator_decision(
        evidence_id: str,
        round_kind: str,
        interaction_payload: Mapping[str, object],
        votes: list[Mapping[str, object]],
        project: str | None = None,
    ) -> dict[str, object]:
        if get_current_agent_run_id() is not None:
            return {
                "ok": False,
                "error": "operator_authentication_required",
                "message": "coordinator_decision rejects agent-capability tokens",
            }
        session_ref = get_current_session_id()
        if session_ref is None:
            return {
                "ok": False,
                "error": "session_required",
                "message": "coordinator_decision requires an operator session",
            }
        project_id = resolve_project_id(project)
        try:
            session_id = sessions.resolve_session_reference(
                session_ref,
                project_id=project_id,
            )
            evidence = evidence_service.record_coordinator_decision(
                evidence_id=evidence_id,
                caller_session_id=session_id,
                round_kind=round_kind,
                interaction_payload=interaction_payload,
                votes=votes,
            )
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        except ValueError as exc:
            return {"ok": False, "error": "invalid_session", "message": str(exc)}
        return {"ok": True, "artifact": evidence.vote_artifact}

    registry.register(
        name="coordinator_decision",
        description=(
            "Record canonical coordinator-authored decisions for an unattended plan round. "
            "Requires an operator-authenticated caller session matching the evidence row; "
            "agent-capability tokens are rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string", "minLength": 1},
                "round_kind": {"type": "string", "enum": sorted(ROUND_KINDS)},
                "interaction_payload": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": PRESENTED_ITEM_SCHEMA,
                        }
                    },
                    "required": ["items"],
                    "additionalProperties": True,
                },
                "votes": {
                    "type": "array",
                    "items": VOTE_SCHEMA,
                },
                "project": {"type": "string"},
            },
            "required": ["evidence_id", "round_kind", "interaction_payload", "votes"],
            "additionalProperties": False,
        },
        func=coordinator_decision,
    )

    def list_plan_vote_artifacts(
        session_id: str | None = None,
        plan_path: str | None = None,
        project: str | None = None,
    ) -> dict[str, object]:
        """List artifacts for an explicit or ambient interactive session."""
        session_ref = session_id or get_current_session_id()
        if session_ref is None:
            return {
                "ok": False,
                "error": "session_required",
                "message": "list_plan_vote_artifacts requires a session",
            }
        project_id = resolve_project_id(project)
        try:
            resolved_session_id = sessions.resolve_session_reference(
                session_ref,
                project_id=project_id,
            )
        except ValueError as exc:
            return {"ok": False, "error": "invalid_session", "message": str(exc)}
        rows = evidence_service.store.list_recent(
            project_id=project_id,
            plan_path=plan_path.strip().replace("\\", "/") if plan_path else None,
            limit=50,
        )
        artifacts = [
            row.vote_artifact
            for row in rows
            if row.session_id == resolved_session_id and row.vote_artifact is not None
        ]
        if plan_path is not None:
            normalized_path = plan_path.strip().replace("\\", "/")
            artifacts = [item for item in artifacts if item.get("plan_path") == normalized_path]
        return {"ok": True, "artifacts": artifacts, "count": len(artifacts)}

    registry.register(
        name="list_plan_vote_artifacts",
        description="List durable structured plan vote artifacts for a same-project session.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Interactive session; defaults to the ambient caller session.",
                },
                "plan_path": {"type": "string"},
                "project": {"type": "string"},
            },
            "additionalProperties": False,
        },
        func=list_plan_vote_artifacts,
    )
