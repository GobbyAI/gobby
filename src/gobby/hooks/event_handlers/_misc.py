from __future__ import annotations

import asyncio
from pathlib import Path

from gobby.app_context import get_app_context
from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse
from gobby.mcp_proxy.tools.worktrees._helpers import (
    copy_project_json_to_worktree,
    generate_worktree_path,
    install_provider_hooks,
    resolve_project_context,
)
from gobby.storage.token_events import build_session_usage_payload
from gobby.utils.project_context import get_workflow_project_path
from gobby.worktrees.git import WorktreeGitManager


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
                    cache_read_tokens = usage.get("cachedContentTokenCount", 0)
                    # total_tokens = usage.get("totalTokenCount", 0)

                    # Update session usage in DB
                    try:
                        self._session_storage.update_usage(
                            session_id=session_id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_creation_tokens=0,  # Gemini doesn't always split these here
                            cache_read_tokens=cache_read_tokens,
                            model=model_name,
                        )
                        self.logger.debug(
                            f"Updated Gemini session usage: {input_tokens} in, {output_tokens} out"
                        )
                        refreshed = self._session_storage.get(session_id)
                        app_ctx = get_app_context()
                        ws_server = app_ctx.websocket_server if app_ctx is not None else None
                        if refreshed is not None and ws_server is not None:
                            payload = build_session_usage_payload(
                                session_id=session_id,
                                project_id=refreshed.project_id,
                                model=model_name if isinstance(model_name, str) else refreshed.model,
                                context_window=refreshed.context_window,
                                totals={
                                    "input_tokens": int(input_tokens or 0),
                                    "output_tokens": int(output_tokens or 0),
                                    "cache_creation_tokens": 0,
                                    "cache_read_tokens": int(cache_read_tokens or 0),
                                },
                            )
                            try:
                                loop = asyncio.get_running_loop()
                                loop.create_task(ws_server.broadcast_session_usage_updated(payload))
                            except RuntimeError:
                                self.logger.debug(
                                    "No running event loop for Gemini usage broadcast",
                                    exc_info=True,
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
        """Handle WORKTREE_CREATE with a git-backed default implementation."""
        worktree_name = event.data.get("name")
        if not isinstance(worktree_name, str) or not worktree_name.strip():
            self.logger.warning("WORKTREE_CREATE missing worktree name")
            return HookResponse(decision="allow")

        git_manager, project_id, error = resolve_project_context(
            event.cwd,
            None,
            None,
        )
        if error or git_manager is None:
            self.logger.warning(f"WORKTREE_CREATE project resolution failed: {error}")
            return HookResponse(decision="allow")

        if self._worktree_manager and project_id:
            existing = self._worktree_manager.get_by_branch(project_id, worktree_name)
            if existing and Path(existing.worktree_path).exists():
                self.logger.info(
                    f"WORKTREE_CREATE reusing existing worktree for '{worktree_name}': "
                    f"{existing.worktree_path}"
                )
                return HookResponse(worktree_path=existing.worktree_path)
            if existing:
                self._worktree_manager.delete(existing.id)

        current_branch = git_manager.get_current_branch()
        base_branch = current_branch or git_manager.get_default_branch()

        use_local = False
        try:
            has_unpushed, _ = git_manager.has_unpushed_commits(base_branch)
            use_local = has_unpushed
        except Exception as e:
            self.logger.debug(f"WORKTREE_CREATE unpushed-commit check failed: {e}")

        worktree_path = generate_worktree_path(worktree_name, Path(git_manager.repo_path).name)
        result = git_manager.create_worktree(
            worktree_path=worktree_path,
            branch_name=worktree_name,
            base_branch=base_branch,
            create_branch=True,
            use_local=use_local,
        )
        if not result.success:
            self.logger.warning(f"WORKTREE_CREATE failed: {result.message}")
            return HookResponse(decision="allow")

        if self._worktree_manager and project_id:
            try:
                self._worktree_manager.create(
                    project_id=project_id,
                    branch_name=worktree_name,
                    worktree_path=worktree_path,
                    base_branch=base_branch,
                )
            except Exception as e:
                self.logger.warning(f"WORKTREE_CREATE record creation failed: {e}")

        try:
            copy_project_json_to_worktree(git_manager.repo_path, worktree_path)
            install_provider_hooks("claude", worktree_path)
        except Exception as e:
            self.logger.warning(f"WORKTREE_CREATE post-setup failed: {e}")

        self.logger.info(f"WORKTREE_CREATE created {worktree_path}")
        return HookResponse(worktree_path=worktree_path)

    def handle_worktree_remove(self, event: HookEvent) -> HookResponse:
        """Handle WORKTREE_REMOVE with git-backed cleanup."""
        worktree_path = event.data.get("worktree_path")
        if not isinstance(worktree_path, str) or not worktree_path.strip():
            self.logger.warning("WORKTREE_REMOVE missing worktree_path")
            return HookResponse(decision="allow")

        repo_path = get_workflow_project_path(Path(worktree_path))
        if repo_path is None and event.cwd:
            repo_path = get_workflow_project_path(Path(event.cwd))
        if repo_path is None:
            repo_path = Path(event.cwd or worktree_path)

        try:
            git_manager = WorktreeGitManager(repo_path)
            result = git_manager.delete_worktree(worktree_path=worktree_path, force=True)
            if not result.success:
                self.logger.warning(f"WORKTREE_REMOVE failed: {result.message}")
            git_delete_succeeded = result.success
        except Exception as e:
            self.logger.warning(f"WORKTREE_REMOVE cleanup failed: {e}")
            git_delete_succeeded = False

        record_cleanup_succeeded = True
        if self._worktree_manager:
            try:
                existing = self._worktree_manager.get_by_path(worktree_path)
                if existing:
                    self._worktree_manager.delete(existing.id)
            except Exception as e:
                self.logger.warning(f"WORKTREE_REMOVE record cleanup failed: {e}")
                record_cleanup_succeeded = False

        if git_delete_succeeded and record_cleanup_succeeded:
            self.logger.info(f"WORKTREE_REMOVE removed {worktree_path}")
        return HookResponse(decision="allow")

    def handle_elicitation(self, event: HookEvent) -> HookResponse:
        """Handle ELICITATION event (Claude Code only)."""
        self._log_observe_only_event("ELICITATION", event)
        return HookResponse(decision="allow")

    def handle_elicitation_result(self, event: HookEvent) -> HookResponse:
        """Handle ELICITATION_RESULT event (Claude Code only)."""
        self._log_observe_only_event("ELICITATION_RESULT", event)
        return HookResponse(decision="allow")
