"""Workflow rule evaluation for hook events."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from gobby.hooks.events import HookEvent, HookResponse
from gobby.telemetry.tracing import create_span

DispatchMcpCalls = Callable[[list[dict[str, Any]], HookEvent], list[dict[str, Any]]]
FormatDiscoveryResult = Callable[[dict[str, Any]], str]

MIN_SKILL_RELEVANCE = 0.65


class WorkflowRuleEvaluator:
    """Evaluate workflow rules and process their MCP side effects."""

    def __init__(
        self,
        *,
        workflow_handler: Any,
        dispatch_mcp_calls: DispatchMcpCalls,
        format_discovery_result: FormatDiscoveryResult,
        database: Any,
        logger: logging.Logger,
    ) -> None:
        self.workflow_handler = workflow_handler
        self.dispatch_mcp_calls = dispatch_mcp_calls
        self.format_discovery_result = format_discovery_result
        self.database = database
        self.logger = logger

    def evaluate(self, event: HookEvent) -> tuple[str | None, HookResponse | None]:
        """Evaluate workflow rules and return context or a blocking response."""
        try:
            with create_span("hook.rules.evaluate"):
                workflow_response = self.workflow_handler.handle(event)

            mcp_calls = (workflow_response.metadata or {}).get("mcp_calls", [])

            with create_span(
                "hook.rules.mcp_dispatch",
                attributes={
                    "mcp_call_count": len(mcp_calls),
                    "mcp_calls": [f"{c.get('server')}/{c.get('tool')}" for c in mcp_calls],
                },
            ):
                dispatch_results = self.dispatch_mcp_calls(mcp_calls, event) if mcp_calls else []

            extra_context: list[str] = []
            block_override = self._process_dispatch_results(
                event,
                dispatch_results,
                extra_context,
            )
            if block_override:
                self._log_workflow_evaluation(event, block_override, mcp_calls)
                return None, block_override

            if workflow_response.decision != "allow":
                self._log_workflow_evaluation(event, workflow_response, mcp_calls)
                if extra_context and workflow_response.context:
                    workflow_response.context = (
                        workflow_response.context + "\n\n" + "\n\n".join(extra_context)
                    )
                elif extra_context:
                    workflow_response.context = "\n\n".join(extra_context)
                return None, workflow_response

            if workflow_response.modified_input:
                event.metadata["_modified_input"] = workflow_response.modified_input
                event.metadata["_auto_approve"] = workflow_response.auto_approve

            self._log_workflow_evaluation(event, workflow_response, mcp_calls)

            workflow_context = workflow_response.context if workflow_response.context else None
            if extra_context:
                heal_context = "\n\n".join(extra_context)
                workflow_context = (
                    f"{workflow_context}\n\n{heal_context}" if workflow_context else heal_context
                )

            return workflow_context, None
        except Exception as exc:
            self.logger.error("Workflow evaluation failed: %s", exc, exc_info=True)
            return None, None

    def _process_dispatch_results(
        self,
        event: HookEvent,
        dispatch_results: list[dict[str, Any]],
        extra_context: list[str],
    ) -> HookResponse | None:
        session_id = event.metadata.get("_platform_session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = None

        for result in dispatch_results:
            if result.get("inject_result") and result.get("result"):
                if result.get("tool") == "search_memories" and session_id:
                    result["result"] = self.dedup_memory_results(result["result"], session_id)
                if result.get("tool") == "search_skills" and session_id:
                    result["result"] = self.dedup_skill_results(result["result"], session_id)
                extra_context.append(self.format_discovery_result(result))

            if result.get("block_on_failure") and not result.get("success"):
                return self._block_for_failed_call(result, extra_context)

            if result.get("block_on_success") and result.get("success"):
                return HookResponse(
                    decision="block",
                    reason=(
                        f"Intercepted by {result['server']}/{result['tool']} "
                        "\u2014 see context below."
                    ),
                    context="\n\n".join(extra_context) if extra_context else None,
                )

        return None

    @staticmethod
    def _block_for_failed_call(
        dispatch_result: dict[str, Any],
        extra_context: list[str],
    ) -> HookResponse:
        result = dispatch_result.get("result") or {}
        error_msg = result.get("error", "unknown") if isinstance(result, dict) else str(result)
        return HookResponse(
            decision="block",
            reason=(
                f"Auto-heal prerequisite failed: "
                f"{dispatch_result['server']}/{dispatch_result['tool']}: {error_msg}"
            ),
            context="\n\n".join(extra_context) if extra_context else None,
        )

    @staticmethod
    def _summarize_mcp_calls(mcp_calls: list[dict[str, Any]]) -> list[str]:
        """Return compact server/tool labels for workflow-triggered MCP calls."""
        targets: list[str] = []
        for call in mcp_calls:
            server = call.get("server")
            tool = call.get("tool")
            if isinstance(server, str) and isinstance(tool, str) and server and tool:
                targets.append(f"{server}/{tool}")
        return targets

    def _log_workflow_evaluation(
        self,
        event: HookEvent,
        workflow_response: HookResponse,
        mcp_calls: list[dict[str, Any]],
    ) -> None:
        """Log workflow decisions, keeping routine allow decisions at debug level."""
        session_id = event.metadata.get("_platform_session_id", "unknown")
        event_name = event.event_type.value
        tool_name = event.data.get("tool_name")
        has_rewrite = bool(workflow_response.modified_input)
        has_captured_or_blocking_mcp_call = any(
            call.get("inject_result")
            or call.get("block_on_failure")
            or call.get("block_on_success")
            for call in mcp_calls
        )
        has_user_visible_response = any(
            (
                workflow_response.system_message,
                workflow_response.permission_decision,
                workflow_response.updated_permissions,
                workflow_response.retry,
                workflow_response.watch_paths,
                workflow_response.worktree_path,
                workflow_response.elicitation_action,
                workflow_response.elicitation_content,
                workflow_response.elicitation_error,
            )
        )

        parts = [
            f"Workflow rule evaluation: event={event_name}",
            f"decision={workflow_response.decision}",
            f"session={session_id}",
        ]
        if isinstance(tool_name, str) and tool_name:
            parts.append(f"tool={tool_name}")
        if mcp_calls:
            parts.append(f"mcp_calls={len(mcp_calls)}")
            mcp_targets = self._summarize_mcp_calls(mcp_calls)
            if mcp_targets:
                parts.append(f"mcp_targets={', '.join(mcp_targets)}")
        if has_rewrite:
            parts.append("rewrote_input=true")
        if workflow_response.auto_approve:
            parts.append("auto_approve=true")
        if workflow_response.reason and workflow_response.decision != "allow":
            parts.append(f"reason={workflow_response.reason}")

        message = ", ".join(parts)
        if (
            workflow_response.decision in ("block", "deny", "ask")
            or has_captured_or_blocking_mcp_call
            or has_user_visible_response
        ):
            self.logger.info(message)
        else:
            self.logger.debug(message)

    def dedup_memory_results(self, result: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Filter already-injected memories and track newly-injected IDs."""
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            sv_mgr = SessionVariableManager(self.database)
            variables = sv_mgr.get_variables(session_id)
            already_injected: set[str] = set(variables.get("injected_memory_ids", []))

            memories = result.get("memories", [])
            id_less = [m for m in memories if not m.get("id")]
            if id_less:
                self.logger.warning(
                    "Memory dedup: %d memories lack 'id' field and cannot be tracked",
                    len(id_less),
                )
            if not memories or not already_injected:
                new_ids = [m["id"] for m in memories if m.get("id")]
                if new_ids:
                    sv_mgr.append_to_set_variable(session_id, "injected_memory_ids", new_ids)
                return result

            filtered = [m for m in memories if m.get("id") not in already_injected]
            new_ids = [m["id"] for m in filtered if m.get("id")]
            if new_ids:
                sv_mgr.append_to_set_variable(session_id, "injected_memory_ids", new_ids)

            return {**result, "memories": filtered}
        except Exception as exc:
            self.logger.debug("Memory injection dedup failed (fail-open): %s", exc)
            return result

    def dedup_skill_results(self, result: dict[str, Any], session_id: str) -> dict[str, Any]:
        """Filter already-suggested skills and low-relevance results."""
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            sv_mgr = SessionVariableManager(self.database)
            variables = sv_mgr.get_variables(session_id)
            already_suggested: set[str] = set(variables.get("suggested_skill_names", []))

            results_list = result.get("results", [])
            if not results_list:
                return result

            filtered = [
                item
                for item in results_list
                if item.get("score", 0) >= MIN_SKILL_RELEVANCE
                and item.get("skill_name", "") not in already_suggested
            ]

            new_names = [item["skill_name"] for item in filtered if item.get("skill_name")]
            if new_names:
                sv_mgr.append_to_set_variable(session_id, "suggested_skill_names", new_names)

            return {**result, "results": filtered, "count": len(filtered)}
        except Exception as exc:
            self.logger.debug("Skill suggestion dedup failed (fail-open): %s", exc)
            return result
