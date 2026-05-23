"""
Hook Event Broadcaster.

Broadcasting of hook events to WebSocket clients with filtering and sanitization.
"""

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from gobby.config.app import DaemonConfig
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.hook_types import (
    HOOK_INPUT_MODELS,
    HOOK_OUTPUT_MODELS,
    HookInput,
    HookOutput,
    HookType,
)

logger = logging.getLogger(__name__)


# Mapping from unified HookEventType to specific HookType Pydantic models
EVENT_TYPE_TO_HOOK_TYPE: dict[str, HookType] = {
    "session_start": HookType.SESSION_START,
    "session_end": HookType.SESSION_END,
    "before_agent": HookType.USER_PROMPT_SUBMIT,
    "after_agent": HookType.STOP,
    "stop": HookType.STOP,
    "stop_failure": HookType.STOP_FAILURE,
    "before_tool": HookType.PRE_TOOL_USE,
    "after_tool": HookType.POST_TOOL_USE,
    "before_tool_selection": HookType.PRE_TOOL_USE,  # Maps to same as before_tool
    "pre_compact": HookType.PRE_COMPACT,
    "post_compact": HookType.POST_COMPACT,
    "subagent_start": HookType.SUBAGENT_START,
    "subagent_stop": HookType.SUBAGENT_STOP,
    "notification": HookType.NOTIFICATION,
    "task_created": HookType.TASK_CREATED,
    "task_completed": HookType.TASK_COMPLETED,
    "teammate_idle": HookType.TEAMMATE_IDLE,
    "instructions_loaded": HookType.INSTRUCTIONS_LOADED,
    "config_change": HookType.CONFIG_CHANGE,
    "cwd_changed": HookType.CWD_CHANGED,
    "file_changed": HookType.FILE_CHANGED,
    "worktree_create": HookType.WORKTREE_CREATE,
    "worktree_remove": HookType.WORKTREE_REMOVE,
    "elicitation": HookType.ELICITATION,
    "elicitation_result": HookType.ELICITATION_RESULT,
    "before_model": HookType.BEFORE_MODEL,
    "after_model": HookType.AFTER_MODEL,
    "permission_request": HookType.PERMISSION_REQUEST,
    "permission_denied": HookType.PERMISSION_DENIED,
}


def schedule_hook_broadcast(
    broadcaster: Any | None,
    event: HookEvent,
    response: HookResponse,
    loop: asyncio.AbstractEventLoop | None,
    hook_logger: logging.Logger,
) -> None:
    """Schedule hook event broadcasting without blocking hook handling."""
    if not broadcaster:
        return

    try:
        running_loop = asyncio.get_running_loop()
        running_loop.create_task(broadcaster.broadcast_event(event, response))
    except RuntimeError:
        if loop:
            coro = broadcaster.broadcast_event(event, response)
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
            except Exception as exc:
                coro.close()
                hook_logger.warning("Failed to schedule broadcast threadsafe: %s", exc)
        else:
            hook_logger.debug("No event loop available for broadcasting")


class HookEventBroadcaster:
    """
    Broadcasts hook events to connected WebSocket clients.

    Handles configuration checking, filtering, payload sanitization,
    and message formatting.
    """

    def __init__(self, websocket_server: Any | None, config: DaemonConfig | None):
        """
        Initialize broadcaster.

        Args:
            websocket_server: WebSocketServer instance (can be None)
            config: Daemon configuration (can be None)
        """
        self.websocket_server = websocket_server
        self.config = config

    @staticmethod
    def _resolve_hook_type(event: HookEvent) -> HookType | None:
        """Resolve the concrete hook type for a unified event."""
        if event.event_type.value == "after_tool" and (
            event.metadata.get("is_failure", False) or event.data.get("is_error", False)
        ):
            return HookType.POST_TOOL_USE_FAILURE
        return EVENT_TYPE_TO_HOOK_TYPE.get(event.event_type.value)

    @staticmethod
    def _is_non_empty_string(value: Any) -> bool:
        """Return True when a value is a non-empty string after trimming whitespace."""
        return isinstance(value, str) and bool(value.strip())

    @classmethod
    def _normalize_post_tool_use_failure_input(cls, raw_input: dict[str, Any]) -> None:
        """Backfill the required top-level error field for failure broadcasts."""
        existing_error = raw_input.get("error")
        if cls._is_non_empty_string(existing_error):
            return
        fallback_error = (
            str(existing_error) if existing_error and not isinstance(existing_error, str) else None
        )

        failure_fields = ("tool_output", "tool_response", "tool_result", "output", "result")

        for field_name in failure_fields:
            candidate = raw_input.get(field_name)
            if not isinstance(candidate, Mapping):
                continue

            nested_error = candidate.get("error")
            if cls._is_non_empty_string(nested_error):
                raw_input["error"] = nested_error
                return

        for field_name in failure_fields:
            candidate = raw_input.get(field_name)
            if cls._is_non_empty_string(candidate):
                raw_input["error"] = candidate
                return

        if fallback_error is not None:
            raw_input["error"] = fallback_error
            return

        raw_input["error"] = "Tool execution failed."

    async def broadcast_event(self, event: HookEvent, response: HookResponse | None = None) -> None:
        """
        Broadcast a unified HookEvent to all connected clients.

        Automatically converts HookEvent to appropriate Pydantic models.

        Args:
            event: The unified HookEvent
            response: Optional HookResponse result
        """
        if not self.websocket_server:
            return

        try:
            # Map unified event type to HookType enum for Pydantic models
            # Use value string lookup to avoid circular imports if HookEventType not available here
            # (Though we imported HookType, we didn't import HookEventType enum class yet, just used strings in dict keys safely)
            enum_hook_type = self._resolve_hook_type(event)

            if not enum_hook_type:
                # Try direct map if values match (fallback)
                try:
                    enum_hook_type = HookType(event.event_type.value)
                except ValueError:
                    logger.warning(
                        f"Skipping broadcast for unknown hook type: {event.event_type.value}"
                    )
                    return

            # Get input/output models
            input_model_cls = HOOK_INPUT_MODELS.get(enum_hook_type)
            output_model_cls = HOOK_OUTPUT_MODELS.get(enum_hook_type)

            if not input_model_cls or not output_model_cls:
                return

            # Prepare input data
            raw_input = event.data.copy()
            # Map 'session_id' -> 'external_id' if needed
            if "external_id" not in raw_input and event.session_id:
                raw_input["external_id"] = event.session_id

            # Special handling for Subagent events: ensure subagent_id is present
            if enum_hook_type in (HookType.SUBAGENT_START, HookType.SUBAGENT_STOP):
                if "subagent_id" not in raw_input and "external_id" in raw_input:
                    raw_input["subagent_id"] = raw_input["external_id"]

            # Map 'prompt' -> 'prompt_text' for UserPromptSubmit
            if enum_hook_type == HookType.USER_PROMPT_SUBMIT:
                if "prompt_text" not in raw_input and "prompt" in raw_input:
                    raw_input["prompt_text"] = raw_input["prompt"]

            # Ensure 'permission_type' has a default for PermissionRequest
            if enum_hook_type == HookType.PERMISSION_REQUEST:
                if "tool_name" not in raw_input:
                    raw_input["tool_name"] = "unknown"
                raw_input.setdefault("tool_input", {})

            # Ensure 'tool_name' has a default for before_tool_selection events
            # These events fire before a specific tool is selected, so tool_name is not available
            if (
                enum_hook_type == HookType.PRE_TOOL_USE
                and event.event_type.value == "before_tool_selection"
            ):
                if "tool_name" not in raw_input:
                    raw_input["tool_name"] = "(tool_selection)"

            if enum_hook_type == HookType.POST_TOOL_USE_FAILURE:
                self._normalize_post_tool_use_failure_input(raw_input)

            # Validate input data structure matches Pydantic model
            # Use construct/model_validate to avoid strict validation errors if possible,
            # or just try/except. Let's rely on standard validation.
            validated_input = input_model_cls(**raw_input)

            # Prepare output data if response provided
            validated_output = None
            if response:
                # Map unified HookResponse to dict that matches Pydantic output model
                # Note: HookResponse is unified, but Pydantic output models vary.
                # Usually outputs have: continue, decision, etc.
                # Simplest is to dump HookResponse to dict and filter/map.

                # Default mapping from HookResponse
                response_dict: dict[str, Any] = {
                    "continue": response.decision not in ("deny", "block"),
                    "decision": response.decision,
                    "stopReason": response.reason,
                    "systemMessage": response.system_message,
                }

                if response.context:
                    if (
                        isinstance(response.context, dict)
                        and "context" in output_model_cls.model_fields
                    ):
                        response_dict["context"] = response.context
                    else:
                        response_dict["additionalContext"] = response.context

                if response.modified_input:
                    response_dict["updatedInput"] = response.modified_input

                if enum_hook_type == HookType.PRE_TOOL_USE:
                    if response.auto_approve:
                        response_dict["permissionDecision"] = "allow"
                    elif response.decision == "ask":
                        response_dict["permissionDecision"] = "ask"
                    elif response.decision in ("deny", "block"):
                        response_dict["permissionDecision"] = "deny"
                    if response.reason and "permissionDecision" in response_dict:
                        response_dict["permissionDecisionReason"] = response.reason

                if enum_hook_type == HookType.PERMISSION_REQUEST:
                    behavior = response.permission_decision
                    if not behavior:
                        if response.decision in ("deny", "block"):
                            behavior = "deny"
                        elif response.decision == "allow":
                            behavior = "allow"
                        elif response.modified_input or response.updated_permissions:
                            behavior = "allow"
                    if behavior:
                        decision_payload: dict[str, Any] = {"behavior": behavior}
                        if behavior == "allow":
                            if response.modified_input:
                                decision_payload["updatedInput"] = response.modified_input
                            if response.updated_permissions:
                                decision_payload["updatedPermissions"] = (
                                    response.updated_permissions
                                )
                        elif response.reason:
                            decision_payload["message"] = response.reason
                        response_dict["decision"] = decision_payload

                if response.retry:
                    response_dict["retry"] = True

                if response.watch_paths is not None:
                    response_dict["watchPaths"] = response.watch_paths

                if response.worktree_path:
                    response_dict["worktreePath"] = response.worktree_path

                if response.elicitation_action:
                    response_dict["action"] = response.elicitation_action
                if response.elicitation_content is not None:
                    response_dict["content"] = response.elicitation_content
                if response.elicitation_error is not None:
                    response_dict["errorMessage"] = response.elicitation_error

                # Clean None values
                response_dict = {k: v for k, v in response_dict.items() if v is not None}

                # Allow pydantic to ignore extra fields
                validated_output = output_model_cls.model_validate(response_dict, strict=False)

            # Call internal broadcast method
            await self.broadcast_hook_event(enum_hook_type, validated_input, validated_output)

        except Exception as e:
            logger.warning(f"Failed to broadcast event {event.event_type}: {e}")

    async def broadcast_hook_event(
        self,
        event_type: HookType,
        event_input: HookInput,
        event_output: HookOutput | None = None,
    ) -> None:
        """
        Broadcast a specific hook event type.

        Args:
            event_type: The type of hook event
            event_input: The input data for the hook
            event_output: The output data from the hook (optional)
        """
        # Checks: WebSocket server implementation required
        if not self.websocket_server:
            return

        # Checks: Feature enabled
        if not self.config:
            return

        ws_config = self.config.hook_extensions.websocket
        if not ws_config.enabled:
            return

        # Checks: Event filtering
        if event_type.value not in ws_config.broadcast_events:
            return

        try:
            # Construct payload
            payload: dict[str, Any] = {
                "type": "hook_event",
                "event_type": event_type.value,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Add input data if enabled
            if ws_config.include_payload:
                # Convert Pydantic model to dict
                input_data = event_input.model_dump(mode="json", exclude_none=True, by_alias=True)

                # Ensuring privacy/security -> stripping potentially sensitive fields could go here

                # Add to payload
                payload["data"] = input_data

                # Add specific fields top-level if needed for convenience
                # e.g. extract session_id from input
                if hasattr(event_input, "external_id"):
                    payload["session_id"] = event_input.external_id
                elif hasattr(event_input, "session_id"):
                    payload["session_id"] = event_input.session_id

            # Add output data if present and enabled
            if event_output and ws_config.include_payload:
                output_data = event_output.model_dump(mode="json", exclude_none=True, by_alias=True)
                payload["result"] = output_data

            # Add task context if present
            if hasattr(event_input, "task_id") and event_input.task_id:
                payload["task_id"] = event_input.task_id
                # Include full task context if available in metadata
                if hasattr(event_input, "metadata") and "_task_context" in event_input.metadata:
                    payload["task_context"] = event_input.metadata["_task_context"]

            # Broadcast message
            await self.websocket_server.broadcast(payload)

        except Exception as e:
            logger.exception(f"Error broadcasting hook event {event_type.value}: {e}")
