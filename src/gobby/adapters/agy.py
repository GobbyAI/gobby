"""AGY CLI hook adapter."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.adapters.agy_contract import (
    AGY_EVENT_MAP,
    AGY_HOOK_ALIASES,
    apply_agy_payload_aliases,
    get_agy_contract,
)
from gobby.adapters.base import normalize_adapter_response_reason
from gobby.adapters.capabilities import ContextChannel
from gobby.adapters.degradation import truncate_context_for_adapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_outcome

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)

_AGY_TOOL_MAP = {
    "list_dir": "Ls",
    "run_command": "Bash",
    "view_file": "Read",
    "find_by_name": "Glob",
    "call_mcp_tool": "mcp__gobby__call_tool",
}


def _join_response_text(*parts: str | None) -> str | None:
    values = [part for part in parts if part]
    if not values:
        return None
    return "\n\n".join(values)


class AgyAdapter(ACPHookAdapter):
    """Adapter for AGY hook translation."""

    EVENT_MAP = dict(AGY_EVENT_MAP)
    HOOK_EVENT_NAME_MAP = dict(AGY_HOOK_ALIASES)
    TOOL_MAP = {**ACPHookAdapter.TOOL_MAP, **_AGY_TOOL_MAP}

    @property
    def source(self) -> SessionSource:
        return SessionSource.AGY

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Alias camelCase AGY payloads, then apply live PostToolUse outcomes."""
        event = super().translate_to_hook_event(self._alias_native_event(native_event))
        invocation_num = event.data.get("invocation_num", 0)
        if event.event_type is HookEventType.BEFORE_AGENT and invocation_num not in (
            0,
            "0",
            None,
        ):
            event.event_type = HookEventType.BEFORE_MODEL
        if event.event_type is HookEventType.AFTER_TOOL:
            event.data.pop("is_error", None)
            normalize_tool_outcome(
                event.data,
                explicit_success=True,
                provenance="agy.hook:PostToolUse",
            )
            event.data["_tool_outcome_locked"] = True
        return event

    @staticmethod
    def _alias_native_event(native_event: dict[str, Any]) -> dict[str, Any]:
        aliased = dict(native_event)
        input_data = aliased.get("input_data")
        if isinstance(input_data, dict):
            aliased["input_data"] = apply_agy_payload_aliases(input_data)
            return aliased
        return apply_agy_payload_aliases(aliased)

    def handle_native(
        self,
        native_event: dict[str, Any],
        hook_manager: HookManager,
    ) -> dict[str, Any]:
        """Dispatch synthetic SESSION_START then the original PreInvocation."""
        hook_type = str(native_event.get("hook_type") or "")
        if not hook_type:
            input_data = native_event.get("input_data")
            if isinstance(input_data, dict):
                hook_type = str(
                    input_data.get("hook_event_name") or input_data.get("hookEventName") or ""
                )
            if not hook_type:
                hook_type = str(
                    native_event.get("hook_event_name") or native_event.get("hookEventName") or ""
                )
        contract = get_agy_contract(hook_type)
        if contract is None or contract.hook_event_name != "PreInvocation":
            return super().handle_native(native_event, hook_manager)

        original = self.translate_to_hook_event(native_event)
        start_event = replace(
            original,
            event_type=HookEventType.SESSION_START,
            metadata={**original.metadata, "_synthetic_session_start": True},
        )
        self._hook_manager = hook_manager
        start_response = hook_manager.handle(start_event)
        original_response = hook_manager.handle(original)
        merged = replace(
            original_response,
            context=_join_response_text(start_response.context, original_response.context),
            system_message=_join_response_text(
                start_response.system_message,
                original_response.system_message,
            ),
        )
        return self.translate_from_hook_response(merged, hook_type="PreInvocation")

    def translate_from_hook_response(
        self,
        response: HookResponse,
        hook_type: str | None = None,
    ) -> dict[str, Any]:
        """Convert HookResponse to AGY protojson stdout.

        AGY unmarshals hook stdout into per-event protobuf messages. Unknown
        fields such as ``continue`` fail the tool. PreToolUse uses ``overwrite``
        for argument rewrites; Stop uses ``decision: continue`` to block stop.
        """

        contract = get_agy_contract(hook_type)
        event_name = contract.hook_event_name if contract is not None else (hook_type or "")
        normalized_reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
            logger=logger,
        )

        if event_name in {"PreInvocation", "PostInvocation"}:
            steps: list[dict[str, str]] = []
            context = response.context
            if context:
                context = truncate_context_for_adapter(
                    context,
                    provider=self.source,
                    hook_type=hook_type,
                    destination_channel=ContextChannel.INJECT_STEPS,
                    contributor_sizes={"response.context": len(context)},
                    event_logger=logger,
                )
                steps.append({"ephemeralMessage": context})
            if response.system_message:
                steps.append({"userMessage": response.system_message})
            result: dict[str, Any] = {}
            if event_name == "PostInvocation" and response.decision in {"deny", "block"}:
                result["terminationBehavior"] = "force_continue"
                if normalized_reason and not any(
                    step.get("ephemeralMessage") == normalized_reason for step in steps
                ):
                    steps.append({"ephemeralMessage": normalized_reason})
            if steps:
                result["injectSteps"] = steps
            return result

        if event_name == "PostToolUse" or contract is None:
            return {}

        if event_name == "Stop":
            if response.decision in {"deny", "block"}:
                stop_result: dict[str, Any] = {"decision": "continue"}
                if normalized_reason:
                    stop_result["reason"] = normalized_reason
                return stop_result
            return {}

        is_denied = response.decision in {"deny", "block"}
        decision: str | None = None
        if response.permission_decision:
            decision = response.permission_decision
        elif response.auto_approve:
            decision = "allow"
        elif response.decision == "ask":
            decision = "ask"
        elif is_denied:
            decision = "deny"

        tool_result: dict[str, Any] = {}
        if decision is not None:
            tool_result["decision"] = decision
        if normalized_reason:
            tool_result["reason"] = normalized_reason
        if response.modified_input is not None:
            tool_result["overwrite"] = response.modified_input
        return tool_result
