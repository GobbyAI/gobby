"""Claude Code adapter for hook translation.

This adapter translates between Claude Code's native hook format and the unified
HookEvent/HookResponse models. It implements the strangler fig pattern for safe
migration from the existing HookManager.execute() method.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.adapters.base import (
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    normalize_adapter_response_reason,
    system_message_has_session_banner,
)
from gobby.adapters.claude_contract import (
    CLAUDE_EVENT_MAP,
    CLAUDE_HOOK_EVENT_NAME_MAP,
    ClaudeDecisionStyle,
    get_claude_contract,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import compress_and_truncate

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)

DECISION_STYLES_ALLOWED_TO_CONTINUE_ON_DENY = frozenset(
    {
        ClaudeDecisionStyle.TOP_LEVEL_BLOCK,
        ClaudeDecisionStyle.PRE_TOOL_USE,
        ClaudeDecisionStyle.PERMISSION_REQUEST,
        ClaudeDecisionStyle.ELICITATION,
        ClaudeDecisionStyle.ELICITATION_RESULT,
        ClaudeDecisionStyle.HARD_STOP,
        ClaudeDecisionStyle.NONE,
    }
)


class ClaudeCodeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI hook translation.

    This adapter:
    1. Translates Claude Code's kebab-case hook payloads to unified HookEvent
    2. Translates HookResponse back to Claude Code's expected format
    3. Calls HookManager.handle() with unified HookEvent model

    Phase 2C Migration Complete:
    - Now using HookManager.handle(HookEvent) for all hooks
    - Legacy execute() path available via set_legacy_mode(True) for rollback
    """

    source = SessionSource.CLAUDE

    # Event type mapping: Claude hook names -> unified HookEventType.
    EVENT_MAP: dict[str, HookEventType] = dict(CLAUDE_EVENT_MAP)
    HOOK_EVENT_NAME_MAP: dict[str, str] = dict(CLAUDE_HOOK_EVENT_NAME_MAP)

    def __init__(self, hook_manager: "HookManager | None" = None):
        """Initialize the Claude Code adapter.

        Args:
            hook_manager: Reference to HookManager for delegation.
                         If None, the adapter can only translate (not handle events).
        """
        self._hook_manager = hook_manager

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Convert Claude Code native event to unified HookEvent.

        Claude Code payloads have the structure:
        {
            "hook_type": "session-start",  # kebab-case hook name
            "input_data": {
                "session_id": "abc123",    # Claude calls this session_id but it's external_id
                "machine_id": "...",
                "cwd": "/path/to/project",
                "transcript_path": "...",
                # ... other hook-specific fields
            }
        }

        Args:
            native_event: Raw payload from Claude Code's ghook-managed hook command

        Returns:
            Unified HookEvent with normalized fields.
        """
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data") or {}

        # Map Claude hook type to unified event type
        # Fall back to NOTIFICATION for unknown types (fail-open)
        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)

        # Extract session_id (Claude calls it session_id but it's the external_id)
        session_id = input_data.get("session_id", "")

        # Normalize event data for CLI-agnostic processing FIRST
        # so that is_error detection (Phase 3) runs before we build metadata
        normalized_data = self._normalize_event_data(input_data)

        # Check for failure: explicit hook type OR inferred from tool output
        is_failure = hook_type == "post-tool-use-failure" or normalized_data.get("is_error", False)
        metadata = {"is_failure": is_failure} if is_failure else {}

        return HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=self.source,
            timestamp=datetime.now(UTC),
            machine_id=input_data.get("machine_id"),
            cwd=input_data.get("cwd"),
            data=normalized_data,
            metadata=metadata,
        )

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Claude Code event data for CLI-agnostic processing.

        Delegates to the shared ``normalize_mcp_fields`` so the same logic
        is used by both the CLI adapter and the web-chat path.

        Args:
            input_data: Raw input data from Claude Code

        Returns:
            Enriched data dict with normalized fields added
        """
        from gobby.hooks.normalization import normalize_tool_fields

        # Copy to avoid mutating the original (shared function mutates in place)
        return normalize_tool_fields(dict(input_data))

    def _build_additional_context(
        self,
        response: HookResponse,
        *,
        hook_type: str | None,
    ) -> str | None:
        """Build Claude ``additionalContext`` content for supported events."""
        contract = get_claude_contract(hook_type)
        if not contract or not contract.allows_additional_context:
            return None

        additional_context_parts: list[str] = []
        session_start_hook = contract.hook_event_name == "SessionStart"

        # SessionStart startup context should be injected once through
        # additionalContext, not duplicated into systemMessage.
        if response.system_message and session_start_hook:
            additional_context_parts.append(response.system_message)

        if response.context:
            additional_context_parts.append(response.context)

        if response.metadata:
            context_lines = build_first_hook_session_metadata_lines(
                response.metadata,
                include_session_id_line=not (
                    session_start_hook
                    and system_message_has_session_banner(response.system_message)
                ),
            )
            if context_lines:
                additional_context_parts.append("\n".join(context_lines))

        if not additional_context_parts:
            return None

        return compress_and_truncate("\n\n".join(additional_context_parts))[0]

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert HookResponse to Claude Code's expected format.

        Claude Code expects responses in this format:
        {
            "continue": True/False,        # Whether to continue execution
            "stopReason": "...",           # Reason if stopped (optional)
            "decision": "approve"/"block", # Tool decision
            "hookSpecificOutput": {        # Hook-specific data
                "hookEventName": "SessionStart",  # Required!
                "additionalContext": "..."  # Context to inject into Claude
            }
        }

        Args:
            response: Unified HookResponse from HookManager.
            hook_type: Original Claude Code hook type (e.g., "session-start")
                      Used to set hookEventName in hookSpecificOutput.

        Returns:
            Dict in Claude Code's expected format.
        """
        contract = get_claude_contract(hook_type)
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
        decision_style = contract.decision_style if contract else ClaudeDecisionStyle.NONE

        if decision_style == ClaudeDecisionStyle.TOP_LEVEL_BLOCK and is_denied:
            result["decision"] = "block"
            if normalized_reason:
                result["reason"] = normalized_reason
        elif decision_style == ClaudeDecisionStyle.PRE_TOOL_USE:
            permission_decision: str | None = response.permission_decision
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
                    if normalized_reason:
                        hook_output["permissionDecisionReason"] = normalized_reason
                if response.modified_input is not None:
                    hook_output["updatedInput"] = response.modified_input
        elif decision_style == ClaudeDecisionStyle.PERMISSION_REQUEST:
            behavior = response.permission_decision
            if not behavior:
                if is_denied:
                    behavior = "deny"
                elif (
                    response.auto_approve
                    or response.modified_input is not None
                    or response.updated_permissions
                ):
                    behavior = "allow"

            if behavior:
                decision_payload: dict[str, Any] = {"behavior": behavior}
                if behavior == "allow":
                    if response.modified_input is not None:
                        decision_payload["updatedInput"] = response.modified_input
                    if response.updated_permissions:
                        decision_payload["updatedPermissions"] = response.updated_permissions
                elif normalized_reason:
                    decision_payload["message"] = normalized_reason
                    if response.decision == "block":
                        decision_payload["interrupt"] = True

                ensure_hook_specific_output()["decision"] = decision_payload
        elif decision_style == ClaudeDecisionStyle.PERMISSION_DENIED:
            if response.retry:
                ensure_hook_specific_output()["retry"] = True
        elif decision_style == ClaudeDecisionStyle.WATCH_PATHS:
            if response.watch_paths is not None:
                ensure_hook_specific_output()["watchPaths"] = response.watch_paths
        elif decision_style == ClaudeDecisionStyle.WORKTREE_CREATE:
            if response.worktree_path:
                ensure_hook_specific_output()["worktreePath"] = response.worktree_path
        elif decision_style == ClaudeDecisionStyle.ELICITATION:
            elicitation_action = response.elicitation_action
            if not elicitation_action and is_denied:
                elicitation_action = "decline"
            if (
                elicitation_action
                or response.elicitation_content is not None
                or response.elicitation_error is not None
            ):
                hook_output = ensure_hook_specific_output()
                if elicitation_action:
                    hook_output["action"] = elicitation_action
                if response.elicitation_content is not None:
                    hook_output["content"] = response.elicitation_content
                if response.elicitation_error is not None:
                    hook_output["errorMessage"] = response.elicitation_error
        elif decision_style == ClaudeDecisionStyle.ELICITATION_RESULT:
            elicitation_action = response.elicitation_action
            if not elicitation_action and is_denied:
                elicitation_action = "decline"
            if elicitation_action or response.elicitation_content is not None:
                hook_output = ensure_hook_specific_output()
                if elicitation_action:
                    hook_output["action"] = elicitation_action
                if response.elicitation_content is not None:
                    hook_output["content"] = response.elicitation_content
        elif decision_style == ClaudeDecisionStyle.HARD_STOP and is_denied:
            result["continue"] = False
            if normalized_reason:
                result["stopReason"] = normalized_reason
        elif decision_style == ClaudeDecisionStyle.NONE and is_denied:
            result["continue"] = False
            if normalized_reason:
                result["stopReason"] = normalized_reason

        if (
            is_denied
            and decision_style not in DECISION_STYLES_ALLOWED_TO_CONTINUE_ON_DENY
            and result.get("continue", True)
        ):
            result["continue"] = False
            if normalized_reason:
                result["stopReason"] = normalized_reason

        if result.get("continue") is False:
            result.pop("decision", None)
            final_hook_output: Any = result.get("hookSpecificOutput")
            if isinstance(final_hook_output, dict):
                for allow_style_key in (
                    "permissionDecision",
                    "updatedInput",
                    "decision",
                    "retry",
                    "watchPaths",
                    "worktreePath",
                    "action",
                    "content",
                    "errorMessage",
                ):
                    final_hook_output.pop(allow_style_key, None)
                if final_hook_output == {"hookEventName": hook_event_name}:
                    result.pop("hookSpecificOutput", None)

        cleanup_hook_output: Any = result.get("hookSpecificOutput")
        if isinstance(cleanup_hook_output, dict) and cleanup_hook_output == {
            "hookEventName": hook_event_name
        }:
            result.pop("hookSpecificOutput", None)

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoint.

        Args:
            native_event: Raw payload from Claude Code's ghook-managed hook command
            hook_manager: HookManager instance for processing.

        Returns:
            Response dict in Claude Code's expected format.
        """
        # Translate to HookEvent
        hook_event = self.translate_to_hook_event(native_event)

        # Use HookEvent-based handler
        hook_type = native_event.get("hook_type", "")
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)
