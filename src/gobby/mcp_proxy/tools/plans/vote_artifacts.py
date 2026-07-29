"""MCP tools for durable interactive plan-round vote artifacts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.vote_artifacts import (
    INTERACTION_TOOLS,
    PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
    ROUND_KINDS,
    VOTE_DECISIONS,
    build_plan_vote_artifact,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.state_manager import SessionVariableManager

_ARTIFACT_VARIABLE = "plan_vote_artifacts"
_MAX_ARTIFACTS = 50

PRESENTED_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding_id": {"type": "string", "minLength": 1},
        "full_item_text": {"type": "string", "minLength": 1},
        "proposed_edit_text": {"type": "string", "minLength": 1},
    },
    "required": ["finding_id", "full_item_text", "proposed_edit_text"],
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

    def record_plan_vote_artifact(
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
            artifact = build_plan_vote_artifact(
                project_id=project_id,
                session_id=session_id,
                plan_path=plan_path,
                round_kind=round_kind,
                round_number=round_number,
                interaction_tool=interaction_tool,
                interaction_payload=interaction_payload,
                votes=votes,
            )
            receipt = variables.get_variables(session_id).get(
                PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE
            )
            _verify_observed_interaction(
                receipt,
                interaction_tool=interaction_tool,
                interaction_payload=artifact["interaction_payload"],
            )
            variables.upsert_bounded_list_variable(
                session_id,
                _ARTIFACT_VARIABLE,
                artifact,
                identity={
                    "plan_path": artifact["plan_path"],
                    "round_kind": round_kind,
                    "round_number": round_number,
                },
                max_items=_MAX_ARTIFACTS,
            )
        except ReviewEvidenceError as exc:
            return exc.to_dict()
        except ValueError as exc:
            return {"ok": False, "error": "invalid_session", "message": str(exc)}
        return {"ok": True, "artifact": artifact}

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
                            "minItems": 1,
                        }
                    },
                    "required": ["items"],
                    "additionalProperties": True,
                },
                "votes": {
                    "type": "array",
                    "items": VOTE_SCHEMA,
                    "minItems": 1,
                },
                "project": {"type": "string"},
            },
            "required": [
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

    def list_plan_vote_artifacts(
        session_id: str | None = None,
        plan_path: str | None = None,
        project: str | None = None,
    ) -> dict[str, object]:
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
        stored = variables.get_variables(resolved_session_id).get(_ARTIFACT_VARIABLE, [])
        artifacts = [item for item in stored if isinstance(item, dict)]
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
                "session_id": {"type": "string"},
                "plan_path": {"type": "string"},
                "project": {"type": "string"},
            },
            "additionalProperties": False,
        },
        func=list_plan_vote_artifacts,
    )


def _verify_observed_interaction(
    receipt: object,
    *,
    interaction_tool: str,
    interaction_payload: object,
) -> None:
    if not isinstance(receipt, Mapping) or receipt.get("response_observed") is not True:
        raise ReviewEvidenceError(
            "plan_vote_interaction_not_observed",
            "Use a native interaction payload and record the artifact immediately after "
            "the user responds; free-text presentation is invalid.",
        )
    if receipt.get("tool") != interaction_tool:
        raise ReviewEvidenceError(
            "plan_vote_interaction_mismatch",
            "The artifact interaction_tool does not match the observed native interaction.",
        )

    observed_payload = receipt.get("payload")
    if not isinstance(observed_payload, Mapping):
        raise ReviewEvidenceError(
            "plan_vote_interaction_payload_mismatch",
            "The observed native interaction did not contain a structured payload.",
        )
    try:
        observed_text = json.dumps(observed_payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ReviewEvidenceError(
            "plan_vote_interaction_payload_mismatch",
            "The observed native interaction payload could not be verified.",
        ) from exc

    if not isinstance(interaction_payload, Mapping):
        raise ReviewEvidenceError(
            "plan_vote_interaction_payload_mismatch",
            "The artifact interaction payload could not be verified.",
        )
    items = interaction_payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ReviewEvidenceError(
            "plan_vote_interaction_payload_mismatch",
            "The artifact interaction items could not be verified.",
        )
    for item in items:
        if not isinstance(item, Mapping):
            raise ReviewEvidenceError(
                "plan_vote_interaction_payload_mismatch",
                "The artifact contains an unverifiable interaction item.",
            )
        for field in ("finding_id", "full_item_text", "proposed_edit_text"):
            value = item.get(field)
            if not isinstance(value, str) or value not in observed_text:
                raise ReviewEvidenceError(
                    "plan_vote_interaction_payload_mismatch",
                    f"Observed interaction payload is missing {field} for a recorded item.",
                )
