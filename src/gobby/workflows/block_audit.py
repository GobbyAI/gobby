"""Shared block-only workflow audit storage."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from gobby.hooks.events import HookEvent
from gobby.storage.workflow_audit import WorkflowAuditManager

logger = logging.getLogger(__name__)

MAX_AUDIT_REASON_LENGTH = 4096


def serialize_audit_condition(condition: Any | None) -> str:
    """Return the canonical audit representation for a rule condition."""
    if condition is None:
        return "-"
    return json.dumps(condition, sort_keys=True, separators=(",", ":"))


async def log_enforcement_block(
    audit_manager: WorkflowAuditManager,
    *,
    session_id: str,
    current_step: Any,
    rule_id: str,
    condition: Any | None,
    result: str,
    reason: str,
    tool_name: str | None,
) -> None:
    """Persist one enforcement block without changing enforcement behavior."""
    step = current_step.strip() if isinstance(current_step, str) else ""
    try:
        await asyncio.to_thread(
            audit_manager.log_rule_eval,
            session_id=session_id,
            step=step or "-",
            rule_id=rule_id,
            condition=serialize_audit_condition(condition),
            result=result,
            reason=reason[:MAX_AUDIT_REASON_LENGTH],
            tool_name=tool_name or "-",
        )
    except Exception as exc:
        logger.warning("Workflow block audit failed: %s", exc, exc_info=True)


async def audit_source_block(
    workflow_handler: Any,
    event: HookEvent,
    *,
    rule_id: str,
    reason: str,
    variables: dict[str, Any] | None = None,
    tool_name: str | None = None,
) -> None:
    """Audit a block created outside RuleEngine."""
    try:
        rule_engine = getattr(workflow_handler, "rule_engine", None)
        if rule_engine is None:
            return
        session_id = event.metadata.get("_platform_session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        event_data = event.data if isinstance(event.data, dict) else {}
        if not tool_name:
            from gobby.workflows.engine.event_utils import _get_tool_identity

            tool_name = _get_tool_identity(event_data)
        await log_enforcement_block(
            rule_engine.workflow_audit,
            session_id=session_id,
            current_step=(variables or {}).get("current_step"),
            rule_id=rule_id,
            condition=None,
            result="block",
            reason=reason,
            tool_name=tool_name,
        )
    except Exception as exc:
        logger.warning("Workflow block audit failed: %s", exc, exc_info=True)


def audit_source_block_sync(
    workflow_handler: Any,
    event: HookEvent,
    *,
    rule_id: str,
    reason: str,
    variables: dict[str, Any] | None = None,
    tool_name: str | None = None,
) -> None:
    """Run source-owned block auditing from synchronous hook paths."""
    try:
        asyncio.run(
            audit_source_block(
                workflow_handler,
                event,
                rule_id=rule_id,
                reason=reason,
                variables=variables,
                tool_name=tool_name,
            )
        )
    except Exception as exc:
        logger.warning("Workflow block audit failed: %s", exc, exc_info=True)
