"""Session verification evidence tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.workflows._resolution import resolve_session_id
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager


def register_verification_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    db: Any,
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
            return {"success": False, "error": "Session manager and database are required"}

        if not summary.strip() or not evidence_type.strip() or not supports.strip():
            return {
                "success": False,
                "error": "summary, evidence_type, and supports must be non-empty",
            }

        if not session_id:
            from gobby.utils.session_context import get_current_session_id

            session_id = get_current_session_id()
        if not session_id:
            return {"success": False, "error": "session_id is required"}

        try:
            resolved_session_id = resolve_session_id(session_manager, session_id)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        evidence = {
            "summary": summary,
            "evidence_type": evidence_type,
            "supports": supports,
            "task_id": task_id,
            "stage_name": stage_name,
            "command": command,
            "scope": scope,
            "timestamp": datetime.now(UTC).isoformat(),
            "tool_name": "record_verification_evidence",
            "success": True,
        }

        manager = SessionVariableManager(db)
        variables = manager.get_variables(resolved_session_id)
        existing = variables.get("verification_evidence", [])
        if not isinstance(existing, list):
            existing = []
        evidence_items = [*existing, evidence]
        manager.merge_variables(
            resolved_session_id,
            {
                "verification_evidence": evidence_items,
                "verification_evidence_recorded": True,
            },
        )

        return {
            "success": True,
            "session_id": resolved_session_id,
            "evidence": evidence,
            "evidence_count": len(evidence_items),
        }
