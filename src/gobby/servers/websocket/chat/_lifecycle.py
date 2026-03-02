"""Hook lifecycle management mixin."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.servers.chat_session import ChatSession

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.workflows.definitions import WorkflowDefinition

logger = logging.getLogger(__name__)


def _inject_agent_skills(
    agent_body: WorkflowDefinition,
    db: DatabaseProtocol,
    project_id: str,
    cli_source: str = "claude_sdk_web_chat",
) -> str | None:
    """Run audience-aware skill injection for an agent definition."""
    from gobby.hooks.event_handlers._session import select_and_format_agent_skills
    from gobby.skills.manager import SkillManager
    from gobby.workflows.selectors import resolve_skills_for_agent

    all_skills = SkillManager(db).list_skills()
    active_skills = resolve_skills_for_agent(agent_body, all_skills)
    formatted, _, _ = select_and_format_agent_skills(
        agent_body, all_skills, active_skills, cli_source
    )
    return formatted


class ChatLifecycleMixin:
    """Lifecycle hook triggers for ChatMixin."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSession]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]

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
            logger.warning("_fire_lifecycle: workflow_handler is None for %s", event_type)
            return None

        # Use the database session ID (not the external conversation_id) so that
        # workflow actions can look up the session via session_manager.get(session_id).
        session = self._chat_sessions.get(conversation_id)
        db_session_id = getattr(session, "db_session_id", None) or conversation_id
        project_path = getattr(session, "project_path", None)

        # Normalize MCP fields using shared logic (same as CLI adapters)
        if data:
            from gobby.hooks.normalization import normalize_tool_fields

            data = normalize_tool_fields(data)

        event = HookEvent(
            event_type=event_type,
            session_id=db_session_id,
            source=SessionSource.CLAUDE_SDK_WEB_CHAT,
            timestamp=datetime.now(UTC),
            data=data,
            metadata={"_platform_session_id": db_session_id},
            cwd=project_path,
        )

        try:
            # DEBUG: log event data to diagnose hook issues
            logger.debug(
                "_fire_lifecycle: %s event_data=%s",
                event_type.name,
                {k: (v if k != "tool_input" else "...") for k, v in (data or {}).items()},
            )
            # WorkflowHookHandler.evaluate is sync (bridges to async internally)
            response: HookResponse = await asyncio.to_thread(workflow_handler.evaluate, event)
            logger.debug(
                "_fire_lifecycle: %s → decision=%s, context_len=%d",
                event_type.name,
                response.decision,
                len(response.context) if response.context else 0,
            )

            # If workflow blocks, return immediately (before webhooks/handlers)
            if response.decision != "allow":
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
                        source="claude_sdk_web_chat",
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
                    logger.debug("_fire_lifecycle: broadcast failed: %s", exc)

            return result
        except Exception as e:
            logger.error("Lifecycle evaluation failed for %s: %s", event_type, e, exc_info=True)
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
                logger.info("Webhook blocked web chat event: %s", reason)
                return {
                    "decision": "block",
                    "context": None,
                    "reason": reason or "Blocked by webhook",
                    "system_message": None,
                }
        except Exception as exc:
            logger.error("Blocking webhook evaluation failed: %s", exc, exc_info=True)
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
            handler_response: HookResponse = await asyncio.to_thread(handler, event)
            if handler_response and handler_response.context:
                return handler_response.context
        except Exception as exc:
            logger.error(
                "_fire_lifecycle: event handler %s failed: %s",
                event_type.name,
                exc,
                exc_info=True,
            )
        return None
