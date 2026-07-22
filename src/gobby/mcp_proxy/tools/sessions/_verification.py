"""Session verification evidence tools."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.workflows._resolution import resolve_session_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.verification_receipts import (
    VerificationReceiptStore,
    VerificationReceiptWrite,
)
from gobby.tasks.verification_outcome_projection import project_verification_outcomes
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.verification_evidence import (
    MAX_VERIFICATION_EVIDENCE_ITEMS,
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE,
    VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW,
    VERIFICATION_EVIDENCE_VARIABLE,
    append_verification_evidence,
    receipt_projection_evidence,
    validate_verification_evidence,
)

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def register_verification_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager | None,
    db: HubDatabase | None,
) -> None:
    """Register manual verification evidence tools."""

    @registry.tool(
        name="record_verification_evidence",
        description=(
            "Record non-command verification evidence for completion readiness. "
            "Requires: summary, evidence_type, supports"
        ),
    )
    def record_verification_evidence(
        summary: str,
        evidence_type: str,
        supports: str,
        task_id: str | None = None,
        stage_name: str | None = None,
        command: str | None = None,
        scope: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Record structured verification evidence for the current session."""
        if session_manager is None or db is None:
            logger.warning(
                "record_verification_evidence missing dependencies: "
                "session_manager_available=%s db_available=%s",
                session_manager is not None,
                db is not None,
            )
            return {"success": False, "error": "Session manager and database are required"}

        summary = summary.strip()
        evidence_type = evidence_type.strip()
        supports = supports.strip()
        if not summary or not evidence_type or not supports:
            return {
                "success": False,
                "error": "summary, evidence_type, and supports must be non-empty",
            }
        if evidence_type != VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW:
            return {
                "success": False,
                "error": "record_verification_evidence only accepts manual_diff_review evidence",
            }
        if not session_id:
            from gobby.utils.session_context import get_current_session_id

            session_id = get_current_session_id()
        if not session_id:
            return {"success": False, "error": "session_id is required"}

        try:
            resolved_session_id = resolve_session_id(session_manager, session_id)
        except ValueError as exc:
            logger.warning(
                "record_verification_evidence failed to resolve session_id=%s",
                session_id,
                exc_info=True,
            )
            return {"success": False, "error": str(exc)}

        recorded_at = datetime.now(UTC)
        evidence = {
            "summary": summary,
            "evidence_type": evidence_type,
            "supports": supports,
            "task_id": task_id,
            "stage_name": stage_name,
            "command": command,
            "scope": scope,
            "timestamp": recorded_at.isoformat(),
            "tool_name": "record_verification_evidence",
            "success": True,
        }
        if error := validate_verification_evidence(evidence):
            return {"success": False, "error": error}
        evidence_item = append_verification_evidence(
            [],
            evidence,
            session_id=resolved_session_id,
        )[0]

        manager = SessionVariableManager(db)
        session = session_manager.get(resolved_session_id)
        if session is None or not session.project_id:
            return {"success": False, "error": "resolved session has no project"}
        receipt_store = VerificationReceiptStore(db)
        variables = manager.get_variables(resolved_session_id)
        active_task_ref = variables.get("active_task_id")
        attributed_task_id, attribution_source = receipt_store.resolve_attribution(
            project_id=session.project_id,
            session_id=resolved_session_id,
            active_task_ref=active_task_ref if isinstance(active_task_ref, str) else None,
            explicit_task_ref=task_id,
        )
        execution_id = f"manual:{uuid.uuid4()}"
        receipt = receipt_store.upsert(
            VerificationReceiptWrite(
                project_id=session.project_id,
                session_id=resolved_session_id,
                task_id=attributed_task_id,
                provider="gobby",
                execution_id=execution_id,
                source_event_id=execution_id,
                evidence_type=evidence_type,
                command=command,
                normalized_outcome="success",
                outcome_provenance="manual_attestation",
                started_at=recorded_at,
                completed_at=recorded_at,
                output=summary,
                details={
                    "summary": summary,
                    "supports": supports,
                    "requested_task_ref": task_id,
                    "stage_name": stage_name,
                    "scope": scope,
                },
                attribution_source=attribution_source,
                attribution_actor=resolved_session_id if attributed_task_id else None,
                attributed_at=recorded_at if attributed_task_id else None,
            )
        )
        evidence_count = manager.append_to_bounded_list_variable(
            resolved_session_id,
            VERIFICATION_EVIDENCE_VARIABLE,
            evidence_item,
            max_items=MAX_VERIFICATION_EVIDENCE_ITEMS,
            updates={
                VERIFICATION_EVIDENCE_RECORDED_VARIABLE: True,
            },
        )
        if attributed_task_id is not None:
            projection = project_verification_outcomes(
                receipt_store.list_for_task(session.project_id, attributed_task_id)
            )
            evidence_count = manager.append_to_bounded_list_variable(
                resolved_session_id,
                VERIFICATION_EVIDENCE_VARIABLE,
                receipt_projection_evidence(projection, task_id=attributed_task_id),
                max_items=MAX_VERIFICATION_EVIDENCE_ITEMS,
                updates={VERIFICATION_EVIDENCE_RECORDED_VARIABLE: projection.ready},
            )
        logger.info(
            "record_verification_evidence merged variables resolved_session_id=%s task_id=%s "
            "evidence_type=%s supports=%s evidence_count=%s",
            resolved_session_id,
            task_id,
            evidence_type,
            supports,
            evidence_count,
        )

        return {
            "success": True,
            "session_id": resolved_session_id,
            "evidence": evidence,
            "evidence_count": evidence_count,
            "receipt_id": receipt.id,
        }


__all__ = [
    "VERIFICATION_EVIDENCE_TYPE_MANUAL_DIFF_REVIEW",
    "register_verification_tools",
]
