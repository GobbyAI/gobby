"""
Hook Event Broadcaster.

Broadcasting of hook events to WebSocket clients with filtering and sanitization.
"""

import logging
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
    "before_tool": HookType.PRE_TOOL_USE,
    "after_tool": HookType.POST_TOOL_USE,
    "pre_compact": HookType.PRE_COMPACT,
    "subagent_start": HookType.SUBAGENT_START,
    "subagent_stop": HookType.SUBAGENT_STOP,
    "notification": HookType.NOTIFICATION,
}


class HookEventBroadcaster:
    """
    Broadcasts hook events to connected WebSocket clients.

    Handles configuration checking, filtering, payload sanitization,
    and message formatting.
    """

    def __init__(self, websocket_server: Any | None, config: DaemonConfig):
        """
        Initialize broadcaster.

        Args:
            websocket_server: WebSocketServer instance (can be None)
            config: Daemon configuration
        """
        self.websocket_server = websocket_server
        self.config = config

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
            enum_hook_type = EVENT_TYPE_TO_HOOK_TYPE.get(event.event_type.value)

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
                response_dict = {
                    "continue": response.decision != "deny",
                    "decision": response.decision,
                    "stopReason": response.reason,
                    "systemMessage": response.system_message,
                }

                # Special handling: hookSpecificOutput from context
                if response.context:
                    # This is tricky without specific model knowledge, but assuming
                    # generic structure or specific model fields.
                    # For SessionStartOutput: { continue: bool, message: str, ... }
                    # SessionStartOutput has: context: dict[str, Any] | None

                    # If model expects 'context' as dict, but we have string.
                    # We identified this mismatch earlier.
                    # If input model expects dict, we need to wrap or parse.
                    # Let's check the fields of the output model.
                    if "context" in output_model_cls.model_fields:
                        if isinstance(response.context, str):
                            # Wrap string in dict if needed, or just pass if model allows str
                            # SessionStartOutput.context is dict[str, Any] | None.
                            response_dict["context"] = {"additionalContext": response.context}
                        else:
                            response_dict["context"] = response.context

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
                input_data = event_input.model_dump(mode="json", exclude_none=True)

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
                output_data = event_output.model_dump(mode="json", exclude_none=True)
                payload["result"] = output_data

            # Add task_id if present
            if hasattr(event_input, "task_id") and event_input.task_id:
                payload["task_id"] = event_input.task_id

            # Broadcast message
            await self.websocket_server.broadcast(payload)

        except Exception as e:
            logger.exception(f"Error broadcasting hook event {event_type.value}: {e}")
