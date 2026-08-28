"""Session end event handler."""

from __future__ import annotations

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.hook_types import SessionEndReason
from gobby.sessions.tmux_context import is_configured_tmux_socket


class SessionEndMixin(EventHandlersBase):
    """Mixin for handling SESSION_END events."""

    def handle_session_end(self, event: HookEvent) -> HookResponse:
        """Handle SESSION_END event."""
        from gobby.workflows.step_instances import AgentStepInstanceManager

        external_id = event.session_id
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug("SESSION_END: session %s", session_id)
        else:
            self.logger.info("SESSION_END: session_id not found for external_id=%s", external_id)

        # If not in mapping, query database
        if not session_id and external_id and self._session_manager:
            self.logger.debug("external_id %s not in mapping, querying database", external_id)
            # Resolve context for lookup
            cwd = event.data.get("cwd")
            project_id = self._resolve_project_id(event.data.get("project_id"), cwd)
            # Lookup with full composite key
            session_id = self._session_manager.lookup_session_id(
                external_id,
                source=event.source.value,
                project_id=project_id,
            )

        # Ensure session_id is available in event metadata for workflow actions
        if session_id and not event.metadata.get("_platform_session_id"):
            event.metadata["_platform_session_id"] = session_id

        # Prevent the liveness monitor from racing this hook's summary/status work.
        liveness_monitor = getattr(self, "_liveness_monitor", None)
        if session_id and liveness_monitor:
            liveness_monitor.mark_recently_handled(session_id)

        # Fetch session once and reuse for auto-link and agent completion
        session = None
        if session_id and self._session_manager:
            try:
                session = self._session_manager.get(session_id)
            except Exception as e:
                self.logger.warning("Failed to fetch session %s: %s", session_id, e)

        try:
            end_reason = SessionEndReason(event.data.get("reason"))
        except (TypeError, ValueError):
            end_reason = SessionEndReason.OTHER
        if end_reason == SessionEndReason.COMPACT:
            end_status = "handoff_ready"
        elif (
            session is not None
            and session.session_type == "terminal"
            and is_configured_tmux_socket(session.terminal_context) is False
        ):
            end_status = "paused"
        elif (
            end_reason == SessionEndReason.IDLE
            and session is not None
            and session.session_type == "web_chat"
        ):
            end_status = "paused"
        else:
            end_status = "expired"
        terminal_outcome = end_status == "expired"

        # Auto-link outside the critical hook executor. The managed worker drains
        # accepted work before HookManager closes shared storage.
        if session and self._session_end_auto_link_worker:
            try:
                from gobby.hooks.session_end_auto_link import SessionEndAutoLinkJob

                self._session_end_auto_link_worker.submit(
                    SessionEndAutoLinkJob(
                        session_id=session.id,
                        project_id=session.project_id,
                        created_at=session.created_at,
                        cwd=event.data.get("cwd"),
                    )
                )
            except Exception as e:
                self.logger.warning(
                    "SESSION_END: failed to enqueue commit auto-link for session %s project %s: %s",
                    session.id,
                    session.project_id,
                    e,
                )

        # Complete agent run if this is a terminal-mode agent session
        if terminal_outcome and session and session.agent_run_id and self._session_coordinator:
            try:
                self._session_coordinator.complete_agent_run(session)
            except Exception as e:
                self.logger.warning("Failed to complete agent run: %s", e)

        # Session-bound workflow instances must be cleared when the session ends
        # so agent-only step enforcement cannot leak onto later requests.
        if (
            terminal_outcome
            and session_id
            and self._workflow_handler
            and self._workflow_handler.rule_engine
        ):
            try:
                deleted_count = AgentStepInstanceManager(
                    self._workflow_handler.rule_engine.db
                ).delete_for_session(session_id)
                if deleted_count > 0:
                    self.logger.info(
                        "SESSION_END: deleted %s agent-step instances for session %s",
                        deleted_count,
                        session_id,
                    )
            except Exception as e:
                self.logger.warning(
                    "SESSION_END: failed to delete agent-step instances for session %s: %s",
                    session_id,
                    e,
                )

        # Unregister from message processor
        message_processor = (
            self._session_message_processors.pop(session_id, None) if session_id else None
        )
        if message_processor is not None and session_id:
            try:
                message_processor.unregister_session(session_id)
            except Exception as e:
                self.logger.warning("Failed to unregister session from message processor: %s", e)

        # Notify pane monitor to prevent double-fire
        if session_id:
            try:
                from gobby.agents.tmux import get_tmux_pane_monitor

                monitor = get_tmux_pane_monitor()
                if monitor:
                    monitor.mark_recently_ended(session_id)
            except Exception as e:
                self.logger.debug("Failed to notify pane monitor for session %s: %s", session_id, e)

        # Release any interactive plan-adversary lock labels owned by this
        # session. The skill's terminal cleanup handles this on every clean
        # exit; this sweep is the safety net for sessions that die before
        # reaching terminal cleanup (browser tab closed, tmux pane killed,
        # daemon crash mid-run). Must be best-effort — session-end must not
        # fail because of a cleanup hiccup.
        if session_id and self._task_manager:
            try:
                lock_label = f"interactive:planning-in-progress:{session_id}"
                stale = self._task_manager.list_tasks(label=lock_label, limit=200)
                for task in stale:
                    try:
                        self._task_manager.remove_label(task.id, lock_label)
                        self.logger.info(
                            "SESSION_END: released interactive-plan lock on task %s (session %s)",
                            task.id,
                            session_id,
                        )
                    except Exception as inner_e:
                        self.logger.warning(
                            "SESSION_END: failed to remove interactive-plan lock on task %s: %s",
                            task.id,
                            inner_e,
                        )
            except Exception as e:
                self.logger.warning(
                    "SESSION_END: orphan-lock sweep failed for session %s: %s", session_id, e
                )

        # Mark as handoff_ready only for explicit handoff exits. Ordinary
        # session ends should expire; live turn completion is handled by
        # AFTER_AGENT/STOP as paused.
        if session_id and self._session_manager:
            try:
                self._session_manager.update_status_if_non_terminal(session_id, end_status)
            except Exception as e:
                self.logger.warning("Failed to update session %s status on end: %s", session_id, e)

        if terminal_outcome and session_id:
            manager = getattr(self, "terminal_manager", None)
            if manager is not None:
                try:
                    row = manager.get_live_for_session(session_id)
                    if row is not None:
                        manager.mark_exited(row.id)
                except Exception as e:
                    self.logger.warning(
                        "SESSION_END: failed to CAS terminal for session %s: %s",
                        session_id,
                        e,
                    )

        return HookResponse(decision="allow")
