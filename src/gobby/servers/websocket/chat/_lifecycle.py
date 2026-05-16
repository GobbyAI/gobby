"""Hook lifecycle management mixin."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.logging_utils import block_tool_name_from_event_data, log_structured_block
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.db import run_db

logger = logging.getLogger(__name__)

_RULE_REASON_RE = re.compile(r"^Rule enforced by Gobby: \[([^\]]+)\]")


def _extract_rule_name(reason: str | None) -> str | None:
    """Extract rule name from standard Gobby rule block prefix."""
    if not reason:
        return None
    match = _RULE_REASON_RE.match(reason)
    if not match:
        return None
    return match.group(1)


def _block_source_for_rule(rule_name: str) -> str:
    """Map workflow block rule names onto observability source labels."""
    if rule_name in {"agent-tool-enforcement", "step-tool-enforcement"}:
        return "step-enforcement"
    return "rule"


def _warn_block_fallback(
    *,
    session_id: str,
    event_type: HookEventType,
    event_data: dict[str, Any],
    source: str,
    rule_name: str,
    detail: str,
) -> None:
    """Emit a warning when lifecycle block handling has to synthesize a reason."""
    logger.warning(
        "BLOCK fallback session=%s event=%s tool=%s source=%s rule=%s detail=%s",
        session_id,
        event_type.value,
        block_tool_name_from_event_data(event_data),
        source,
        rule_name,
        detail,
    )


class ChatLifecycleMixin:
    """Lifecycle hook triggers for ChatMixin."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSessionProtocol]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]
    _pending_projects: dict[str, str]

    if TYPE_CHECKING:

        async def _send_error(
            self,
            websocket: Any,
            message: str,
            request_id: str | None = None,
            code: str = "ERROR",
        ) -> None: ...

        async def broadcast_session_event(
            self,
            event: str,
            session_id: str,
            **kwargs: Any,
        ) -> None: ...

        def _inject_pending_messages(
            self,
            db_session_id: str,
            event_type: HookEventType,
        ) -> str | None: ...

        async def _cancel_active_chat(self, conversation_id: str) -> None: ...

    async def _fire_lifecycle(
        self,
        conversation_id: str,
        event_type: HookEventType,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Bridge SDK hook events to workflow engine lifecycle triggers.

        Mirrors HookManager.handle() for CLI parity:
        1. Rule evaluation via workflow_handler
        2. Blocking webhook evaluation
        3. MCP call dispatch for rule effects
        4. Event handler dispatch (skill interception, etc.)
        5. Inter-session message piggyback (BEFORE_TOOL/AFTER_TOOL)
        6. Event broadcasting for audit trail

        Returns a dict with HookResponse fields (decision, context, reason, etc.)
        or None if no workflow handler is available.
        """
        workflow_handler = getattr(self, "workflow_handler", None)
        if not workflow_handler:
            logger.warning(f"_fire_lifecycle: workflow_handler is None for {event_type}")
            return None

        # Use the database session ID (not the external conversation_id) so that
        # workflow actions can look up the session via session_manager.get(session_id).
        session = self._chat_sessions.get(conversation_id)
        db_session_id = getattr(session, "db_session_id", None) or conversation_id
        project_path = getattr(session, "project_path", None)
        project_id = getattr(session, "project_id", None)

        # Normalize MCP fields using shared logic (same as CLI adapters)
        if data:
            from gobby.hooks.normalization import normalize_tool_fields

            data = normalize_tool_fields(data)

        # Source is determined by session's provider (default claude)
        provider_value = getattr(session, "provider", "claude")
        try:
            source = SessionSource(provider_value)
        except ValueError:
            logger.warning(
                "Invalid session provider %r; defaulting lifecycle source to 'claude'",
                provider_value,
            )
            source = SessionSource("claude")

        metadata: dict[str, Any] = {
            "_platform_session_id": db_session_id,
            "session_type": "web_chat",
        }
        if project_path:
            metadata["project_path"] = project_path

        event = HookEvent(
            event_type=event_type,
            session_id=db_session_id,
            source=source,
            timestamp=datetime.now(UTC),
            data=data,
            metadata=metadata,
            cwd=project_path,
            project_id=project_id,
        )

        try:
            # DEBUG: log event data to diagnose hook issues
            redacted_event_data = {
                key: (value if key != "tool_input" else "...")
                for key, value in (data or {}).items()
            }
            logger.debug(
                "_fire_lifecycle: %s event_data=%s",
                event_type.name,
                redacted_event_data,
            )
            # WorkflowHookHandler.evaluate is sync (bridges to async internally)
            response: HookResponse = await run_db(self, workflow_handler.evaluate, event)
            logger.debug(
                f"_fire_lifecycle: {event_type.name} → decision={response.decision}, context_len={(len(response.context) if response.context else 0)}",
            )

            # If workflow blocks, return immediately (before webhooks/handlers)
            if response.decision != "allow":
                if response.decision == "block":
                    rule_name = _extract_rule_name(response.reason) or "workflow-lifecycle"
                    block_source = _block_source_for_rule(rule_name)
                    reason = (response.reason or "").strip()
                    if not reason:
                        _warn_block_fallback(
                            session_id=db_session_id,
                            event_type=event_type,
                            event_data=event.data,
                            source=block_source,
                            rule_name=rule_name,
                            detail="workflow handler omitted block reason",
                        )
                        reason = (
                            "Workflow lifecycle blocked this event without providing a "
                            "reason. Inspect workflow block handling."
                        )
                        response.reason = reason
                    log_structured_block(
                        logger,
                        session_id=db_session_id,
                        event=event_type.value,
                        tool=block_tool_name_from_event_data(event.data),
                        source=block_source,
                        rule=rule_name,
                        reason=reason,
                    )
                return {
                    "decision": response.decision,
                    "context": response.context,
                    "reason": response.reason,
                    "system_message": response.system_message,
                }

            # --- Blocking webhook evaluation (parity with CLI path D1) ---
            webhook_block = await self._evaluate_blocking_webhooks(event)
            if webhook_block:
                return webhook_block

            # Dispatch mcp_call effects from rule engine (parity with CLI path)
            mcp_calls = (response.metadata or {}).get("mcp_calls", [])
            if mcp_calls:
                await self._dispatch_mcp_calls(mcp_calls, event)

            # Dispatch to event handler (parity with CLI HookManager.handle)
            # This is where skill interception lives (handle_before_agent)
            handler_context = await self._dispatch_event_handlers(event_type, event)

            # Merge handler context with rule engine context
            merged_context = response.context
            if handler_context:
                if merged_context:
                    merged_context = merged_context + "\n\n" + handler_context
                else:
                    merged_context = handler_context

            # --- Inter-session message piggyback (parity with CLI path D6) ---
            msg_context = self._inject_pending_messages(db_session_id, event_type)
            if msg_context:
                if merged_context:
                    merged_context = merged_context + "\n\n" + msg_context
                else:
                    merged_context = msg_context

            # Build result dict
            result: dict[str, Any] = {
                "decision": response.decision,
                "context": merged_context,
                "reason": response.reason,
                "system_message": response.system_message,
            }

            # --- Input rewriting (parity with hook_manager.py:387-390) ---
            if response.modified_input:
                result["modified_input"] = response.modified_input
                result["auto_approve"] = response.auto_approve

            # Session context enrichment (parity with CLI adapter)
            if session and getattr(session, "seq_num", None):
                session_ref = f"#{session.seq_num}"
                ctx = result.get("context")
                if event_type == HookEventType.PRE_COMPACT:
                    # Richer context for compaction survival
                    from gobby.servers.chat_session_helpers import build_compaction_context

                    enrichment = build_compaction_context(
                        session_ref=session_ref,
                        project_id=getattr(session, "project_id", None),
                        cwd=project_path,
                        source="claude",
                    )
                else:
                    enrichment = f"Gobby Session ID: {session_ref}"
                result["context"] = f"{enrichment}\n\n{ctx}" if ctx else enrichment

            # --- Event broadcasting for audit trail (parity with CLI path D2) ---
            hook_broadcaster = getattr(self, "hook_broadcaster", None)
            if hook_broadcaster:
                try:
                    await hook_broadcaster.broadcast_event(event, response)
                except Exception as exc:
                    logger.debug(f"_fire_lifecycle: broadcast failed: {exc}")

            # --- Non-blocking webhook dispatch (parity with hook_manager.py:442-446) ---
            await self._dispatch_non_blocking_webhooks(event)

            return result
        except Exception as e:
            # Defensive fail-open for web-chat lifecycle hooks. PreCompact runs
            # on the SDK compaction path; hook failures must not abort the
            # compaction itself or strand the active conversation.
            logger.error(f"Lifecycle evaluation failed for {event_type}: {e}", exc_info=True)
            return None

    async def _evaluate_blocking_webhooks(
        self,
        event: HookEvent,
    ) -> dict[str, Any] | None:
        """Evaluate blocking webhooks before handler execution.

        Async-native version of HookManager._evaluate_blocking_webhooks
        for the web chat path. Returns a block result dict if a webhook
        blocked the event, None otherwise.
        """
        webhook_dispatcher = getattr(self, "webhook_dispatcher", None)
        if not webhook_dispatcher:
            return None

        if not webhook_dispatcher.config.enabled:
            return None

        try:
            # Filter to blocking endpoints that match this event
            matching_endpoints = [
                ep
                for ep in webhook_dispatcher.config.endpoints
                if ep.enabled
                and webhook_dispatcher._matches_event(ep, event.event_type.value)
                and ep.can_block
            ]

            if not matching_endpoints:
                return None

            # Build payload and dispatch
            payload = webhook_dispatcher._build_payload(event)
            results = []
            for endpoint in matching_endpoints:
                result = await webhook_dispatcher._dispatch_single(endpoint, payload)
                results.append(result)

            decision, reason = webhook_dispatcher.get_blocking_decision(results)
            if decision == "block":
                resolved_reason = (reason or "").strip()
                if not resolved_reason:
                    _warn_block_fallback(
                        session_id=event.session_id,
                        event_type=event.event_type,
                        event_data=event.data,
                        source="webhook",
                        rule_name="webhook-dispatch",
                        detail="blocking webhook omitted reason",
                    )
                    resolved_reason = (
                        "Blocking webhook denied this web chat event without providing "
                        "a reason. Inspect webhook responses for the blocking endpoint."
                    )
                log_structured_block(
                    logger,
                    session_id=event.session_id,
                    event=event.event_type.value,
                    tool=block_tool_name_from_event_data(event.data),
                    source="webhook",
                    rule="webhook-dispatch",
                    reason=resolved_reason,
                )
                return {
                    "decision": "block",
                    "context": None,
                    "reason": resolved_reason,
                    "system_message": None,
                }
        except Exception as exc:
            logger.error(f"Blocking webhook evaluation failed: {exc}", exc_info=True)
            # Fail-open for webhook errors

        return None

    async def _dispatch_mcp_calls(
        self,
        mcp_calls: list[dict[str, Any]],
        event: HookEvent,
    ) -> None:
        """Dispatch MCP calls defined in rule effects."""
        mcp_manager = getattr(self, "mcp_manager", None)
        if not mcp_manager:
            return

        from gobby.hooks.mcp_dispatch import dispatch_mcp_calls

        internal_mgr = getattr(self, "internal_manager", None)

        async def _call_tool(server: str, tool: str, arguments: dict[str, Any]) -> Any:
            """Route to internal registries first, then external."""
            if internal_mgr and internal_mgr.is_internal(server):
                registry = internal_mgr.get_registry(server)
                if registry:
                    return await registry.call(tool, arguments)
            return await mcp_manager.call_tool(server, tool, arguments)

        await dispatch_mcp_calls(mcp_calls, event, _call_tool, logger)

    async def _dispatch_event_handlers(
        self,
        event_type: HookEventType,
        event: HookEvent,
    ) -> str | None:
        """Dispatch to CLI event handlers and return their context."""
        event_handlers = getattr(self, "event_handlers", None)
        if not event_handlers:
            return None

        handler = event_handlers.get_handler(event_type)
        if not handler:
            return None

        try:
            handler_response: HookResponse = await run_db(self, handler, event)
            if handler_response and handler_response.context:
                return handler_response.context
        except Exception as exc:
            logger.error(
                f"_fire_lifecycle: event handler {event_type.name} failed: {exc}",
                exc_info=True,
            )
        return None

    async def _dispatch_non_blocking_webhooks(self, event: HookEvent) -> None:
        """Dispatch non-blocking webhooks (fire-and-forget).

        Mirrors HookManager._dispatch_webhooks_async for CLI parity.
        Filters to non-blocking endpoints (opposite of _evaluate_blocking_webhooks).
        """
        webhook_dispatcher = getattr(self, "webhook_dispatcher", None)
        if not webhook_dispatcher or not webhook_dispatcher.config.enabled:
            return

        try:
            matching_endpoints = [
                ep
                for ep in webhook_dispatcher.config.endpoints
                if ep.enabled
                and webhook_dispatcher._matches_event(ep, event.event_type.value)
                and not ep.can_block
            ]
            if not matching_endpoints:
                return

            payload = webhook_dispatcher._build_payload(event)
            tasks = [webhook_dispatcher._dispatch_single(ep, payload) for ep in matching_endpoints]
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.warning(f"Non-blocking webhook dispatch failed: {exc}")
