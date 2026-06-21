"""Audit helpers for step workflow enforcement."""

from __future__ import annotations

import json
from typing import Any

from gobby.storage.workflow_audit import WorkflowAuditManager


class EnforcementAuditMixin:
    """Audit helper methods used by step workflow enforcement."""

    workflow_audit: WorkflowAuditManager

    @staticmethod
    def _audit_value(value: Any) -> Any:
        try:
            json.dumps(value)
            return value
        except TypeError:
            return repr(value)

    @staticmethod
    def _step_audit_context(
        workflow: str,
        step: str,
        *,
        mcp_key: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        context = {"workflow": workflow, "step": step}
        if mcp_key:
            context["mcp_key"] = mcp_key
        for key, value in extra.items():
            if value is not None:
                context[key] = EnforcementAuditMixin._audit_value(value)
        return context

    def _audit_step_tool_call(
        self,
        session_id: str,
        workflow: str,
        step: str,
        tool_name: str,
        result: str,
        *,
        reason: str | None = None,
        mcp_key: str | None = None,
    ) -> None:
        self.workflow_audit.log_tool_call(
            session_id=session_id,
            step=step,
            tool_name=mcp_key or tool_name,
            result=result,
            reason=reason,
            context=self._step_audit_context(workflow, step, mcp_key=mcp_key),
        )

    def _audit_step_set_variable(
        self,
        session_id: str,
        workflow: str,
        step: str,
        mcp_key: str,
        variable: str,
        value: Any,
    ) -> None:
        self.workflow_audit.log(
            session_id=session_id,
            step=step,
            event_type="set_variable",
            result="set",
            reason=f"Set workflow variable '{variable}'",
            context=self._step_audit_context(
                workflow,
                step,
                mcp_key=mcp_key,
                variable=variable,
                value=value,
            ),
        )
