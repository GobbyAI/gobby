"""Droid CLI adapter for hook translation.

Translates between Factory Droid's hook payload format and Gobby's unified
HookEvent/HookResponse models. Standalone by design: it does not inherit from
any other CLI adapter.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, cast

from gobby.adapters.base import (
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    normalize_adapter_response_reason,
    system_message_has_session_banner,
)
from gobby.adapters.capabilities import ContextChannel, get_provider_capabilities
from gobby.adapters.degradation import (
    AdapterDegradationKind,
    persist_kwargs_from_hook_response,
    record_adapter_degradation,
    record_unsupported_response_fields,
    truncate_context_for_adapter,
)
from gobby.adapters.droid_contract import (
    DROID_EVENT_MAP,
    DROID_HOOK_EVENT_NAME_MAP,
    DroidDecisionStyle,
    get_droid_contract,
)
from gobby.hooks.events import (
    HookEvent,
    HookEventType,
    HookResponse,
    SessionSource,
    correlate_hook_lifecycle,
)
from gobby.sessions.transcripts.droid import (
    droid_transcript_confirms_turn_cancellation,
    droid_transcript_has_tool_id,
)

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)


def _normalize_router_help(prompt: str) -> str:
    """Recover help from Droid's native managed-skill expansion."""
    match = re.fullmatch(
        r"<system-notification>\n"
        r"Skills provide specialized capabilities and domain knowledge\. "
        r"The user has selected the following skill for immediate execution\. "
        r"Begin following the skill's instructions now\.\n"
        r'<skill filePath="[^"\n]+/skills/gobby/SKILL\.md">\n'
        r"<name>gobby</name>\n<description>[^\n]*</description>\n"
        r"# Gobby Router\n.*?\n<!-- gobby-router:end -->\n"
        r"</skill>\n</system-notification>\s*(help)?\s*",
        prompt,
        re.DOTALL,
    )
    if match is None:
        return prompt
    return "/gobby help" if match.group(1) else "/gobby"


class DroidAdapter(BaseAdapter):
    """Adapter for Factory Droid hook translation."""

    @property
    def source(self) -> SessionSource:
        return SessionSource.DROID

    EVENT_MAP: dict[str, HookEventType] = dict(DROID_EVENT_MAP)
    HOOK_EVENT_NAME_MAP: dict[str, str] = dict(DROID_HOOK_EVENT_NAME_MAP)

    def __init__(self, hook_manager: HookManager | None = None) -> None:
        self._hook_manager = hook_manager

    def _resolve_hook_type(self, native_event: dict[str, Any]) -> str:
        hook_type = native_event.get("hook_type")
        if isinstance(hook_type, str) and hook_type:
            return hook_type

        input_data = native_event.get("input_data") or {}
        if isinstance(input_data, dict):
            hook_event_name = input_data.get("hook_event_name")
            if isinstance(hook_event_name, str) and hook_event_name:
                return hook_event_name

        hook_event_name = native_event.get("hook_event_name")
        if isinstance(hook_event_name, str):
            return hook_event_name
        return ""

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Convert Droid native payload to a unified HookEvent."""

        hook_type = self._resolve_hook_type(native_event)
        raw_input_data = native_event.get("input_data", {}) or {}
        input_data = raw_input_data if isinstance(raw_input_data, dict) else {}

        if not input_data and "hook_event_name" in native_event:
            input_data = native_event

        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)
        normalized_data = self._normalize_event_data(input_data)
        if event_type is HookEventType.BEFORE_AGENT:
            prompt = normalized_data.get("prompt")
            if isinstance(prompt, str):
                normalized_data["prompt"] = _normalize_router_help(prompt)
        session_id = self._resolve_session_id(native_event, input_data)
        normalized_session_id = normalized_data.get("session_id")
        normalized_session_id = (
            normalized_session_id.strip() if isinstance(normalized_session_id, str) else ""
        )
        if normalized_session_id:
            normalized_data["session_id"] = normalized_session_id
            session_id = normalized_session_id
        elif session_id:
            normalized_data["session_id"] = session_id
        if event_type is HookEventType.SESSION_START and not session_id:
            logger.warning(
                "Droid SessionStart missing session id",
                extra={
                    "native_keys": sorted(native_event.keys()),
                    "input_data_keys": sorted(input_data.keys()),
                },
            )
        is_failure = normalized_data.get("is_error")
        metadata: dict[str, Any] = {"_native_hook_type": hook_type}
        if hook_type == "PostToolUse":
            metadata["is_failure"] = is_failure if isinstance(is_failure, bool) else False
        elif hook_type == "PostToolUseFailure":
            metadata["is_failure"] = True
        elif is_failure is True:
            metadata["is_failure"] = True
        if "is_failure" in metadata:
            from gobby.hooks.normalization import normalize_tool_outcome

            normalize_tool_outcome(
                normalized_data,
                explicit_success=not metadata["is_failure"],
                provenance=f"droid.hook:{hook_type}",
            )
        self._copy_platform_session_metadata(native_event, metadata)

        event = HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=self.source,
            timestamp=self._hook_event_timestamp(native_event),
            machine_id=self._resolve_text_field(
                native_event, input_data, "machine_id", "machineId"
            ),
            cwd=self._resolve_text_field(native_event, input_data, "cwd"),
            data=normalized_data,
            metadata=metadata,
        )
        correlate_hook_lifecycle(event)
        if hook_type == "Stop":
            event.turn_disposition = "completed"
        elif hook_type == "Notification" and event.wait_token:
            notification_kind = normalized_data.get(
                "notification_type",
                normalized_data.get("type", normalized_data.get("kind")),
            )
            if isinstance(notification_kind, str):
                normalized_kind = notification_kind.casefold()
                transcript_path = normalized_data.get("transcript_path")
                interaction_visible = droid_transcript_has_tool_id(
                    transcript_path if isinstance(transcript_path, str) else None,
                    event.wait_token,
                )
                if interaction_visible and normalized_kind in {
                    "ask_user_question",
                    "elicitation",
                    "question",
                }:
                    event.wait_kind = "input"
                elif interaction_visible and normalized_kind in {
                    "permission_prompt",
                    "permission_request",
                    "approval",
                }:
                    event.wait_kind = "approval"
        elif hook_type == "Notification":
            notification_kind = normalized_data.get(
                "notification_type",
                normalized_data.get("type", normalized_data.get("kind")),
            )
            reason = normalized_data.get("reason")
            transcript_path = normalized_data.get("transcript_path")
            if (
                notification_kind == "idle_prompt"
                and reason in {"cancelled", "canceled", "user_interrupt"}
                and droid_transcript_confirms_turn_cancellation(
                    transcript_path if isinstance(transcript_path, str) else None,
                    event.provider_turn_key,
                )
            ):
                event.event_type = HookEventType.INTERRUPT
                event.turn_disposition = "user_interrupted"
        return event

    def _resolve_session_id(
        self,
        native_event: dict[str, Any],
        input_data: dict[str, Any],
    ) -> str:
        """Resolve Droid's external session id from supported payload shapes."""
        return self._resolve_text_field(native_event, input_data, "session_id", "sessionId") or ""

    def _resolve_text_field(
        self,
        native_event: dict[str, Any],
        input_data: dict[str, Any],
        *field_names: str,
    ) -> str | None:
        for source in (input_data, native_event):
            for field_name in field_names:
                value = source.get(field_name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Droid event data for CLI-agnostic processing."""

        from gobby.hooks.normalization import normalize_tool_fields

        explicit_is_error = input_data.get("is_error")
        normalized_input = dict(input_data)
        aliases = {
            "transcriptPath": "transcript_path",
            "notificationType": "notification_type",
            "turnId": "turn_id",
            "promptId": "prompt_id",
            "toolCallId": "tool_call_id",
        }
        for native_name, canonical_name in aliases.items():
            if native_name in normalized_input and canonical_name not in normalized_input:
                normalized_input[canonical_name] = normalized_input[native_name]
        if normalized_input.get("tool_name") == "Execute":
            normalized_input["tool_name"] = "Bash"
        normalized = normalize_tool_fields(normalized_input)
        if not isinstance(explicit_is_error, bool):
            normalized.pop("is_error", None)
        prompt = normalized.get("prompt")
        user_prompt = normalized.get("user_prompt")
        if not prompt and isinstance(user_prompt, str) and user_prompt:
            normalized["prompt"] = user_prompt
        return normalized

    def _build_additional_context(
        self,
        response: HookResponse,
        *,
        hook_type: str | None,
    ) -> str | None:
        """Build Droid additionalContext content for supported events."""

        contract = get_droid_contract(hook_type)
        capabilities = get_provider_capabilities(self.source)
        capability = capabilities.get_hook(hook_type)
        if (
            not contract
            or not capability
            or capability.context_channel is not ContextChannel.ADDITIONAL_CONTEXT
        ):
            return None

        parts: list[tuple[str, str]] = []
        session_start_hook = contract.hook_event_name == "SessionStart"

        if response.system_message and session_start_hook:
            parts.append(("system_message", response.system_message))

        if response.context:
            parts.append(("response.context", response.context))

        if response.metadata:
            context_lines = build_first_hook_session_metadata_lines(
                response.metadata,
                include_session_id_line=not (
                    session_start_hook
                    and system_message_has_session_banner(response.system_message)
                ),
            )
            if context_lines:
                parts.append(("metadata", "\n".join(context_lines)))

        if not parts:
            return None

        return truncate_context_for_adapter(
            "\n\n".join(part for _, part in parts),
            provider=self.source,
            hook_type=hook_type,
            destination_channel=ContextChannel.ADDITIONAL_CONTEXT,
            contributor_sizes={label: len(part) for label, part in parts},
            event_logger=logger,
            **persist_kwargs_from_hook_response(response, self._hook_manager),
        )

    def _deny_reason_with_context(
        self,
        response: HookResponse,
        context: str,
        reason: str | None,
        *,
        hook_type: str | None,
    ) -> str:
        """Fold context into a denial: Droid's PreToolUse shows the model only the reason."""
        record_adapter_degradation(
            provider=self.source,
            hook_type=hook_type,
            kind=AdapterDegradationKind.REROUTED_FIELD,
            response_field="context",
            destination_channel="permissionDecisionReason",
            event_logger=logger,
        )
        bounded = truncate_context_for_adapter(
            context,
            provider=self.source,
            hook_type=hook_type,
            destination_channel="permissionDecisionReason",
            contributor_sizes={"response.context": len(context)},
            event_logger=logger,
            **persist_kwargs_from_hook_response(response, self._hook_manager),
        )
        return "\n\n".join(part for part in (reason, bounded) if part)

    def translate_from_hook_response(
        self,
        response: HookResponse,
        hook_type: str | None = None,
    ) -> dict[str, Any]:
        """Convert HookResponse to Droid's expected hook JSON shape."""

        contract = get_droid_contract(hook_type)
        capabilities = get_provider_capabilities(self.source)
        capability = capabilities.get_hook(hook_type)
        record_unsupported_response_fields(
            response,
            provider=self.source,
            hook_type=hook_type,
            capability=capability,
            event_logger=logger,
        )
        hook_event_name = contract.hook_event_name if contract else "Unknown"
        additional_context = self._build_additional_context(response, hook_type=hook_type)

        result: dict[str, Any] = {"continue": True}
        if response.system_message and hook_event_name != "SessionStart":
            result["systemMessage"] = response.system_message

        def ensure_hook_specific_output() -> dict[str, Any]:
            hook_output = result.setdefault(
                "hookSpecificOutput",
                {"hookEventName": hook_event_name},
            )
            return cast(dict[str, Any], hook_output)

        if additional_context:
            ensure_hook_specific_output()["additionalContext"] = additional_context

        is_denied = response.decision in ("deny", "block")
        normalized_reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
            logger=logger,
        )
        decision_style = contract.decision_style if contract else DroidDecisionStyle.NONE

        if decision_style == DroidDecisionStyle.TOP_LEVEL_BLOCK and is_denied:
            result["decision"] = "block"
            if normalized_reason:
                result["reason"] = normalized_reason
        elif decision_style == DroidDecisionStyle.PRE_TOOL_USE:
            # First-block-wins: a permission "allow" or auto_approve from a
            # higher-priority rule must not override a lower-priority block
            # (#16670, which fixed only the Claude adapter).
            permission_decision: str | None = "deny" if is_denied else response.permission_decision
            if not permission_decision:
                if response.auto_approve:
                    permission_decision = "allow"
                elif response.decision == "ask":
                    permission_decision = "ask"
                elif is_denied:
                    permission_decision = "deny"

            if (
                permission_decision
                or response.modified_input is not None
                or normalized_reason
                or additional_context
            ):
                hook_output = ensure_hook_specific_output()
                if permission_decision:
                    hook_output["permissionDecision"] = permission_decision
                    decision_reason = normalized_reason
                    if (
                        permission_decision == "deny"
                        and response.context
                        and not additional_context
                    ):
                        decision_reason = self._deny_reason_with_context(
                            response, response.context, normalized_reason, hook_type=hook_type
                        )
                    if decision_reason:
                        hook_output["permissionDecisionReason"] = decision_reason
                if response.modified_input is not None and permission_decision != "deny":
                    hook_output["updatedInput"] = response.modified_input
        elif decision_style == DroidDecisionStyle.NONE and is_denied and normalized_reason:
            result.setdefault("systemMessage", normalized_reason)

        cleanup_hook_output: Any = result.get("hookSpecificOutput")
        if isinstance(cleanup_hook_output, dict) and cleanup_hook_output == {
            "hookEventName": hook_event_name
        }:
            result.pop("hookSpecificOutput", None)

        return result

    def handle_native(
        self,
        native_event: dict[str, Any],
        hook_manager: HookManager,
    ) -> dict[str, Any]:
        """Translate, handle through HookManager, and translate the response."""

        hook_event = self.translate_to_hook_event(native_event)
        hook_type = self._resolve_hook_type(native_event)
        self._hook_manager = hook_manager
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)
