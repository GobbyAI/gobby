"""AGY CLI hook adapter."""

from __future__ import annotations

import logging
from typing import Any

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.adapters.agy_contract import AGY_EVENT_MAP, AGY_HOOK_ALIASES, get_agy_contract
from gobby.adapters.base import normalize_adapter_response_reason
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

logger = logging.getLogger(__name__)


class AgyAdapter(ACPHookAdapter):
    """Adapter for AGY hook translation."""

    EVENT_MAP = dict(AGY_EVENT_MAP)
    HOOK_EVENT_NAME_MAP = dict(AGY_HOOK_ALIASES)

    @property
    def source(self) -> SessionSource:
        return SessionSource.AGY

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Keep AGY tool outcomes unknown until its live result contract is proven."""
        event = super().translate_to_hook_event(native_event)
        if event.event_type is HookEventType.AFTER_TOOL:
            event.data.pop("is_error", None)
            event.data["tool_outcome"] = {
                "status": "unknown",
                "provenance": "agy.provider_contract_unproven",
            }
            event.data["_tool_outcome_locked"] = True
        return event

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
            if response.context:
                steps.append({"ephemeralMessage": response.context})
            if response.system_message:
                steps.append({"userMessage": response.system_message})
            return {"injectSteps": steps} if steps else {}

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

        result: dict[str, Any] = {}
        if decision is not None:
            result["decision"] = decision
        if normalized_reason:
            result["reason"] = normalized_reason
        if response.modified_input is not None:
            result["overwrite"] = response.modified_input
        return result
