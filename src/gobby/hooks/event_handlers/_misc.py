from __future__ import annotations

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse


class MiscEventHandlerMixin(EventHandlersBase):
    """Mixin for handling miscellaneous events."""

    def _log_observe_only_event(self, event_name: str, event: HookEvent) -> None:
        """Log an observe-only Claude event without side effects."""
        session_id = event.metadata.get("_platform_session_id")
        if session_id:
            self.logger.debug(f"{event_name}: session {session_id}")
        else:
            self.logger.debug(event_name)

    def handle_notification(self, event: HookEvent) -> HookResponse:
        """Handle NOTIFICATION event."""
        input_data = event.data
        notification_type = (
            input_data.get("notification_type")
            or input_data.get("notificationType")
            or input_data.get("type")
            or "general"
        )
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"NOTIFICATION ({notification_type}): session {session_id}")
            if self._session_manager:
                try:
                    self._session_manager.update_session_status(session_id, "paused")
                except Exception as e:
                    self.logger.warning(f"Failed to update session status: {e}")
        else:
            self.logger.debug(f"NOTIFICATION ({notification_type})")

        return HookResponse(decision="allow")

    def handle_permission_request(self, event: HookEvent) -> HookResponse:
        """Handle PERMISSION_REQUEST event (Claude Code only)."""
        input_data = event.data
        session_id = event.metadata.get("_platform_session_id")
        permission_type = input_data.get("permission_type", "unknown")

        if session_id:
            self.logger.debug(f"PERMISSION_REQUEST ({permission_type}): session {session_id}")
        else:
            self.logger.debug(f"PERMISSION_REQUEST ({permission_type})")

        return HookResponse(decision="allow")

    def handle_before_model(self, event: HookEvent) -> HookResponse:
        """Handle BEFORE_MODEL event (Gemini only)."""
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"BEFORE_MODEL: session {session_id}")
        else:
            self.logger.debug("BEFORE_MODEL")

        return HookResponse(decision="allow")

    def handle_after_model(self, event: HookEvent) -> HookResponse:
        """Handle AFTER_MODEL event (Gemini only)."""
        session_id = event.metadata.get("_platform_session_id")
        input_data = event.data

        if session_id:
            self.logger.debug(f"AFTER_MODEL: session {session_id}")

            # Extract usage metadata from response
            # Gemini CLI payload structure: {"response": {"usageMetadata": {...}}, "model_name": "..."}
            response_data = input_data.get("response")
            model_name = input_data.get("model_name") or input_data.get("model")

            if isinstance(response_data, dict) and self._session_storage:
                usage = response_data.get("usageMetadata")
                if usage:
                    input_tokens = usage.get("promptTokenCount", 0)
                    output_tokens = usage.get("candidatesTokenCount", 0)
                    # total_tokens = usage.get("totalTokenCount", 0)

                    # Update session usage in DB
                    try:
                        self._session_storage.update_usage(
                            session_id=session_id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_creation_tokens=0,  # Gemini doesn't always split these here
                            cache_read_tokens=0,
                            model=model_name,
                        )
                        self.logger.debug(
                            f"Updated Gemini session usage: {input_tokens} in, {output_tokens} out"
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to update Gemini session usage: {e}")
        else:
            self.logger.debug("AFTER_MODEL")

        return HookResponse(decision="allow")

    def handle_permission_denied(self, event: HookEvent) -> HookResponse:
        """Handle PERMISSION_DENIED event (Claude Code only)."""
        self._log_observe_only_event("PERMISSION_DENIED", event)
        return HookResponse(decision="allow")

    def handle_post_compact(self, event: HookEvent) -> HookResponse:
        """Handle POST_COMPACT event (Claude Code only)."""
        self._log_observe_only_event("POST_COMPACT", event)
        return HookResponse(decision="allow")

    def handle_stop_failure(self, event: HookEvent) -> HookResponse:
        """Handle STOP_FAILURE event (Claude Code only)."""
        self._log_observe_only_event("STOP_FAILURE", event)
        return HookResponse(decision="allow")

    def handle_task_created(self, event: HookEvent) -> HookResponse:
        """Handle TASK_CREATED event (Claude Code only)."""
        self._log_observe_only_event("TASK_CREATED", event)
        return HookResponse(decision="allow")

    def handle_task_completed(self, event: HookEvent) -> HookResponse:
        """Handle TASK_COMPLETED event (Claude Code only)."""
        self._log_observe_only_event("TASK_COMPLETED", event)
        return HookResponse(decision="allow")

    def handle_teammate_idle(self, event: HookEvent) -> HookResponse:
        """Handle TEAMMATE_IDLE event (Claude Code only)."""
        self._log_observe_only_event("TEAMMATE_IDLE", event)
        return HookResponse(decision="allow")

    def handle_instructions_loaded(self, event: HookEvent) -> HookResponse:
        """Handle INSTRUCTIONS_LOADED event (Claude Code only)."""
        self._log_observe_only_event("INSTRUCTIONS_LOADED", event)
        return HookResponse(decision="allow")

    def handle_config_change(self, event: HookEvent) -> HookResponse:
        """Handle CONFIG_CHANGE event (Claude Code only)."""
        self._log_observe_only_event("CONFIG_CHANGE", event)
        return HookResponse(decision="allow")

    def handle_cwd_changed(self, event: HookEvent) -> HookResponse:
        """Handle CWD_CHANGED event (Claude Code only)."""
        self._log_observe_only_event("CWD_CHANGED", event)
        return HookResponse(decision="allow")

    def handle_file_changed(self, event: HookEvent) -> HookResponse:
        """Handle FILE_CHANGED event (Claude Code only)."""
        self._log_observe_only_event("FILE_CHANGED", event)
        return HookResponse(decision="allow")

    def handle_worktree_create(self, event: HookEvent) -> HookResponse:
        """Handle WORKTREE_CREATE event (Claude Code only)."""
        self._log_observe_only_event("WORKTREE_CREATE", event)
        return HookResponse(decision="allow")

    def handle_worktree_remove(self, event: HookEvent) -> HookResponse:
        """Handle WORKTREE_REMOVE event (Claude Code only)."""
        self._log_observe_only_event("WORKTREE_REMOVE", event)
        return HookResponse(decision="allow")

    def handle_elicitation(self, event: HookEvent) -> HookResponse:
        """Handle ELICITATION event (Claude Code only)."""
        self._log_observe_only_event("ELICITATION", event)
        return HookResponse(decision="allow")

    def handle_elicitation_result(self, event: HookEvent) -> HookResponse:
        """Handle ELICITATION_RESULT event (Claude Code only)."""
        self._log_observe_only_event("ELICITATION_RESULT", event)
        return HookResponse(decision="allow")
