from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.workflows.definitions import AgentDefinitionBody

_derive_logger = logging.getLogger(__name__)


@dataclass
class AgentActivationResult:
    """Result of activating the default agent for a session."""

    context: str | None  # AI-only: preamble + formatted skills
    agent_name: str
    description: str | None
    role: str | None
    goal: str | None
    rules_count: int
    skills_count: int
    variables_count: int
    injected_skill_names: list[str]  # skills with format "full" or "content"


def select_and_format_agent_skills(
    agent_body: AgentDefinitionBody,
    all_skills: list[Any],
    active_skills: set[str] | None,
    cli_source: str,
) -> tuple[str | None, int, list[str]]:
    """Audience-aware skill selection and formatting for agent activation.

    Shared between session activation and web chat skill injection.

    Args:
        agent_body: Resolved agent definition body
        all_skills: All skills from the database
        active_skills: Set of active skill names (None = all eligible)
        cli_source: CLI source name for audience filtering

    Returns:
        (formatted_text, skills_count, injected_skill_names)
    """
    from gobby.hooks.skill_manager import _db_skill_to_parsed
    from gobby.skills.formatting import _format_skills_with_formats
    from gobby.skills.injector import AgentContext, SkillInjector, SkillProfile

    eligible = (
        all_skills if active_skills is None else [s for s in all_skills if s.name in active_skills]
    )
    parsed = [_db_skill_to_parsed(s) for s in eligible if s.enabled]
    if not parsed:
        return None, 0, []

    agent_ctx = AgentContext(
        agent_depth=0, has_human=True, agent_type="interactive", source=cli_source
    )
    profile = None
    if agent_body.workflows and agent_body.workflows.skill_format:
        profile = SkillProfile(default_format=agent_body.workflows.skill_format)

    selected = SkillInjector().select_skills(parsed, agent_ctx, profile=profile)
    if not selected:
        return None, 0, []

    injected_names = [skill.name for skill, fmt in selected if fmt in ("full", "content")]
    formatted = _format_skills_with_formats(selected)
    return formatted, len(selected), injected_names


class SessionEventHandlerMixin(EventHandlersBase):
    """Mixin for handling session-related events."""

    def _derive_transcript_path(
        self, cli_source: str, input_data: dict[str, Any], external_id: str
    ) -> str | None:
        """Derive transcript path for CLIs that don't provide one natively.

        Args:
            cli_source: CLI source name (gemini, cursor, etc.)
            input_data: Hook event input data
            external_id: External session ID

        Returns:
            Path to transcript file, or None if not derivable.
        """
        if cli_source in ("gemini", "antigravity"):
            return self._find_gemini_transcript(input_data, external_id)
        if cli_source == "cursor":
            return self._find_cursor_transcript(input_data, external_id)
        return None

    def _find_gemini_transcript(self, input_data: dict[str, Any], external_id: str) -> str | None:
        """Find the Gemini session JSON file.

        Gemini stores sessions at:
        ~/.gemini/tmp/{SHA256(cwd)}/chats/session-{date}T{time}-{session_id[:8]}.json
        """
        cwd = input_data.get("cwd")
        if not cwd:
            self.logger.debug("Cannot derive Gemini transcript: no cwd")
            return None

        session_id = input_data.get("session_id") or external_id or ""
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = Path.home() / ".gemini" / "tmp" / project_hash / "chats"

        if not chats_dir.exists():
            self.logger.debug(f"Gemini chats dir not found: {chats_dir}")
            return None

        # Try to match by session_id prefix (first 8 chars)
        prefix = session_id[:8] if session_id else ""
        if prefix:
            matches = sorted(chats_dir.glob(f"session-*-{prefix}.json"), reverse=True)
            if matches:
                self.logger.debug(f"Found Gemini transcript by prefix: {matches[0]}")
                return str(matches[0])

        # Fallback: most recent session file
        all_sessions = sorted(chats_dir.glob("session-*.json"), reverse=True)
        if all_sessions:
            self.logger.debug(f"Found Gemini transcript (most recent): {all_sessions[0]}")
            return str(all_sessions[0])

        self.logger.debug(f"No Gemini session files in {chats_dir}")
        return None

    def _find_cursor_transcript(self, input_data: dict[str, Any], external_id: str) -> str | None:
        """Find the Cursor NDJSON capture file.

        For spawned Cursor agents, Gobby writes stdout to a capture file.
        The path is passed via GOBBY_CURSOR_CAPTURE_PATH env var or
        stored in the session's jsonl_path.
        """
        terminal_context = input_data.get("terminal_context")
        if terminal_context:
            capture_path = terminal_context.get("cursor_capture_path")
            if capture_path:
                self.logger.debug(f"Found Cursor capture path from context: {capture_path}")
                return str(capture_path)

        # Check for capture file in standard location
        session_id = input_data.get("session_id") or external_id or ""
        if not session_id or not re.match(r"^[a-zA-Z0-9._-]+$", session_id):
            return None
        std_path = f"{tempfile.gettempdir()}/gobby-cursor-{session_id}.ndjson"
        if Path(std_path).exists():
            self.logger.debug(f"Found Cursor capture file: {std_path}")
            return std_path

        return None

    def handle_session_start(self, event: HookEvent) -> HookResponse:
        """
        Handle SESSION_START event.

        Register session and execute session-handoff workflow.
        """
        external_id = event.session_id
        input_data = event.data
        transcript_path = input_data.get("transcript_path")
        cli_source = event.source.value
        cwd = input_data.get("cwd")

        # Derive transcript path for CLIs that don't provide one natively
        if not transcript_path:
            transcript_path = self._derive_transcript_path(cli_source, input_data, external_id)
        session_source = input_data.get("source", "startup")

        # Resolve project_id (auto-creates if needed)
        project_id = self._resolve_project_id(input_data.get("project_id"), cwd)
        # Always use Gobby's machine_id for cross-CLI consistency
        machine_id = self._get_machine_id()

        self.logger.debug(
            f"SESSION_START: cli={cli_source}, project={project_id}, source={session_source}"
        )

        # Step 0: Check if this is a pre-created session (terminal mode agent)
        # Two cases:
        # 1. Claude: We pass --session-id <internal_id>, so external_id IS our internal ID
        # 2. Gemini: We pass GOBBY_SESSION_ID env var, hook_dispatcher includes it in terminal_context
        existing_session = None
        terminal_context = input_data.get("terminal_context")
        gobby_session_id_from_env = (
            terminal_context.get("gobby_session_id") if terminal_context else None
        )

        if self._session_storage:
            try:
                # Try to find by internal ID first (Claude case - external_id IS internal_id)
                existing_session = self._session_storage.get(external_id)
                if existing_session:
                    return self._handle_pre_created_session(
                        existing_session=existing_session,
                        external_id=external_id,
                        transcript_path=transcript_path,
                        cli_source=cli_source,
                        event=event,
                        cwd=cwd,
                    )
            except Exception as e:
                self.logger.debug(f"No pre-created session found by external_id: {e}")

            # Gemini case: Look up by gobby_session_id from terminal_context
            if gobby_session_id_from_env and not existing_session:
                try:
                    existing_session = self._session_storage.get(gobby_session_id_from_env)
                    if existing_session:
                        self.logger.info(
                            f"Found pre-created session {gobby_session_id_from_env} via "
                            f"terminal_context, updating external_id to {external_id}"
                        )
                        # Update the session's external_id and terminal_context
                        # terminal_context contains parent_pid needed for kill_agent
                        self._session_storage.update(
                            gobby_session_id_from_env,
                            external_id=external_id,
                            terminal_context=terminal_context,
                        )
                        # Cache mapping so subsequent hooks skip DB lookup
                        if self._session_manager:
                            self._session_manager.cache_session_mapping(
                                external_id=external_id,
                                source=cli_source,
                                session_id=gobby_session_id_from_env,
                            )
                        return self._handle_pre_created_session(
                            existing_session=existing_session,
                            external_id=external_id,
                            transcript_path=transcript_path,
                            cli_source=cli_source,
                            event=event,
                            cwd=cwd,
                        )
                except Exception as e:
                    self.logger.debug(f"No pre-created session found by gobby_session_id: {e}")

        # Step 1: Find parent session
        # Check env vars first (spawned agent case), then handoff (source='clear')
        parent_session_id = input_data.get("parent_session_id")
        workflow_name = input_data.get("workflow_name")
        agent_depth = input_data.get("agent_depth")

        if not parent_session_id and self._session_storage:
            try:
                parent = self._session_storage.find_parent(
                    machine_id=machine_id,
                    project_id=project_id,
                    source=cli_source,
                    status="handoff_ready",
                )

                # Race condition: Claude Code fires session-start before session-end,
                # so the old session may still be active when we look for handoff_ready.
                # SESSION_END is fast (just sets status), so a short backoff suffices.
                if not parent and session_source in ("clear", "compact"):
                    import time

                    deadline = time.monotonic() + 15  # 15s — session_end is fast now
                    while time.monotonic() < deadline:
                        time.sleep(2)
                        parent = self._session_storage.find_parent(
                            machine_id=machine_id,
                            project_id=project_id,
                            source=cli_source,
                            status="handoff_ready",
                        )
                        if parent:
                            self.logger.debug(
                                f"Found handoff_ready parent after backoff: {parent.id}"
                            )
                            break
                    if not parent:
                        self.logger.warning(
                            f"No handoff_ready parent found for /{session_source} session"
                        )

                if parent:
                    parent_session_id = parent.id
                    self.logger.debug(f"Found parent session: {parent_session_id}")
                    # Read handoff_source marker set by prepare-clear-handoff
                    # or preserve-context-on-compact rules
                    from gobby.workflows.state_manager import SessionVariableManager

                    parent_vars = SessionVariableManager(self._session_storage.db).get_variables(
                        parent.id
                    )
                    handoff_source = parent_vars.get("handoff_source")
                    if handoff_source in ("clear", "compact"):
                        session_source = handoff_source
                        input_data["source"] = session_source
            except Exception as e:
                self.logger.warning(f"Error finding parent session: {e}")

        # Step 2: Register new session with parent if found
        # terminal_context already extracted in Step 0
        # Parse agent_depth as int if provided
        agent_depth_val = 0
        if agent_depth:
            try:
                agent_depth_val = int(agent_depth)
            except (ValueError, TypeError):
                pass

        session_id = None
        if self._session_manager:
            session_id = self._session_manager.register_session(
                external_id=external_id,
                machine_id=machine_id,
                project_id=project_id,
                parent_session_id=parent_session_id,
                jsonl_path=transcript_path,
                source=cli_source,
                project_path=cwd,
                terminal_context=terminal_context,
                workflow_name=workflow_name,
                agent_depth=agent_depth_val,
            )

        # Step 2b: Mark parent session as expired after successful handoff
        if parent_session_id and self._session_manager:
            try:
                self._session_manager.mark_session_expired(parent_session_id)
                self.logger.debug(f"Marked parent session {parent_session_id} as expired")
            except Exception as e:
                self.logger.warning(f"Failed to mark parent session as expired: {e}")

        # Step 2c: Pipeline workflows are executed by the agent via run_pipeline MCP tool.
        # Agent rules enforce pipeline execution by blocking all tools except
        # progressive disclosure and run_pipeline.
        if workflow_name and session_id:
            self.logger.debug(
                "Pipeline workflow registered for session — agent will execute via run_pipeline",
                extra={"workflow_name": workflow_name, "session_id": session_id},
            )

        # Step 2d: Deep load default agent (rules, skills, variables) for new session
        agent_result: AgentActivationResult | None = None
        if session_id:
            try:
                agent_override = input_data.get("agent_name_override")
                agent_result = self._activate_default_agent(
                    session_id,
                    cli_source,
                    project_id,
                    agent_name_override=agent_override,
                )
            except Exception as e:
                self.logger.error(f"Failed to activate default agent: {e}", exc_info=True)

        # Step 3: Track registered session
        if transcript_path and self._session_coordinator:
            try:
                self._session_coordinator.register_session(external_id)
            except Exception as e:
                self.logger.error(f"Failed to setup session tracking: {e}", exc_info=True)

        # Step 4: Update event metadata with the newly registered session_id
        event.metadata["_platform_session_id"] = session_id
        if parent_session_id:
            event.metadata["_parent_session_id"] = parent_session_id

        # Step 5: Register with Message Processor
        if self._message_processor and transcript_path and session_id:
            try:
                self._message_processor.register_session(
                    session_id, transcript_path, source=cli_source
                )
            except Exception as e:
                self.logger.warning(f"Failed to register session with message processor: {e}")

        # Build additional context (agent AI context + task context)
        additional_context: list[str] = []
        if agent_result and agent_result.context:
            additional_context.append(agent_result.context)

        # Populate handoff session variables for inject_context rule templates
        if parent_session_id and session_id and self._session_storage:
            from gobby.workflows.state_manager import SessionVariableManager

            sv_mgr = SessionVariableManager(self._session_storage.db)
            current_vars = sv_mgr.get_variables(session_id)
            if current_vars.get("auto_inject_handoff", True):
                parent = self._session_storage.get(parent_session_id)
                if parent:
                    # For /clear: generate boundary summaries now (no pre-clear hook
                    # fires in Claude Code, so summaries don't exist yet).
                    # For /compact: PRE_COMPACT already generated compact_markdown.
                    if session_source == "clear" and not parent.summary_markdown:
                        if self._dispatch_boundary_summaries_fn:
                            try:
                                self._dispatch_boundary_summaries_fn(parent_session_id, False)
                            except Exception as e:
                                self.logger.warning(
                                    f"Failed to generate boundary summaries "
                                    f"for parent {parent_session_id}: {e}"
                                )
                            # Re-read parent after generation
                            parent = self._session_storage.get(parent_session_id)

                    handoff_vars: dict[str, Any] = {}
                    if parent and session_source == "clear" and parent.summary_markdown:
                        handoff_vars["full_session_summary"] = parent.summary_markdown
                    elif parent and session_source == "compact" and parent.compact_markdown:
                        handoff_vars["compact_session_summary"] = parent.compact_markdown
                    if handoff_vars:
                        sv_mgr.merge_variables(session_id, handoff_vars)

                    # Preserve task claim state across compaction/clear
                    parent_vars = sv_mgr.get_variables(parent_session_id)
                    if session_source in ("compact", "clear"):
                        _TASK_CLAIM_KEYS = (
                            "task_claimed",
                            "claimed_task_id",
                            "task_ref",
                            "session_had_task",
                        )
                        task_handoff = {
                            k: parent_vars[k] for k in _TASK_CLAIM_KEYS if parent_vars.get(k)
                        }
                        if task_handoff:
                            sv_mgr.merge_variables(session_id, task_handoff)
                            # Re-assign task and re-link to new session
                            if task_handoff.get("task_claimed") and task_handoff.get(
                                "claimed_task_id"
                            ):
                                claimed_id = task_handoff["claimed_task_id"]
                                if self._task_manager:
                                    try:
                                        self._task_manager.update_task(
                                            claimed_id, assignee=session_id
                                        )
                                    except Exception as e:
                                        self.logger.debug(
                                            f"Best-effort task re-assignment failed for session={session_id} task={claimed_id}: {e}"
                                        )
                                if self._session_task_manager:
                                    try:
                                        self._session_task_manager.link_task(
                                            session_id, claimed_id, "claimed"
                                        )
                                    except Exception as e:
                                        self.logger.debug(
                                            f"Best-effort session-task link failed for session={session_id} task={claimed_id}: {e}"
                                        )

        # Populate task_context session variable for inject_context rule templates
        if event.task_id and session_id and self._session_storage:
            task_title = event.metadata.get("_task_title", "Unknown Task")
            task_context_str = f"You are working on task: {task_title} ({event.task_id})"
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(self._session_storage.db).merge_variables(
                session_id, {"task_context": task_context_str}
            )

        if event.task_id:
            task_title = event.metadata.get("_task_title", "Unknown Task")
            additional_context.append("\n## Active Task Context\n")
            additional_context.append(f"You are working on task: {task_title} ({event.task_id})")

        # Fetch session to get seq_num for #N display
        session_obj = None
        if session_id and self._session_storage:
            session_obj = self._session_storage.get(session_id)

        return self._compose_session_response(
            session=session_obj,
            session_id=session_id,
            external_id=external_id,
            parent_session_id=parent_session_id,
            machine_id=machine_id,
            project_id=project_id,
            task_id=event.task_id,
            additional_context=additional_context,
            terminal_context=terminal_context,
            agent_info=agent_result,
            session_source=session_source,
        )

    def handle_session_end(self, event: HookEvent) -> HookResponse:
        """Handle SESSION_END event."""
        from gobby.tasks.commits import auto_link_commits

        external_id = event.session_id
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"SESSION_END: session {session_id}")
        else:
            self.logger.warning(f"SESSION_END: session_id not found for external_id={external_id}")

        # If not in mapping, query database
        if not session_id and external_id and self._session_manager:
            self.logger.debug(f"external_id {external_id} not in mapping, querying database")
            # Resolve context for lookup
            machine_id = self._get_machine_id()
            cwd = event.data.get("cwd")
            project_id = self._resolve_project_id(event.data.get("project_id"), cwd)
            # Lookup with full composite key
            session_id = self._session_manager.lookup_session_id(
                external_id,
                source=event.source.value,
                machine_id=machine_id,
                project_id=project_id,
            )

        # Ensure session_id is available in event metadata for workflow actions
        if session_id and not event.metadata.get("_platform_session_id"):
            event.metadata["_platform_session_id"] = session_id

        # Auto-link commits made during this session to tasks
        if session_id and self._session_storage and self._task_manager:
            try:
                session = self._session_storage.get(session_id)
                if session:
                    cwd = event.data.get("cwd")
                    link_result = auto_link_commits(
                        task_manager=self._task_manager,
                        since=session.created_at,
                        cwd=cwd,
                    )
                    if link_result.total_linked > 0:
                        self.logger.info(
                            f"Auto-linked {link_result.total_linked} commits to tasks: "
                            f"{list(link_result.linked_tasks.keys())}"
                        )
            except Exception as e:
                self.logger.warning(f"Failed to auto-link session commits: {e}")

        # Complete agent run if this is a terminal-mode agent session
        if session_id and self._session_storage and self._session_coordinator:
            try:
                session = self._session_storage.get(session_id)
                if session and session.agent_run_id:
                    self._session_coordinator.complete_agent_run(session)
            except Exception as e:
                self.logger.warning(f"Failed to complete agent run: {e}")

        # Unregister from message processor
        if self._message_processor and (session_id or external_id):
            try:
                target_id = session_id or external_id
                self._message_processor.unregister_session(target_id)
            except Exception as e:
                self.logger.warning(f"Failed to unregister session from message processor: {e}")

        # Notify pane monitor to prevent double-fire
        if session_id:
            try:
                from gobby.agents.tmux import get_tmux_pane_monitor

                monitor = get_tmux_pane_monitor()
                if monitor:
                    monitor.mark_recently_ended(session_id)
            except Exception as e:
                self.logger.debug(f"Failed to notify pane monitor for session {session_id}: {e}")

        # Mark as handoff_ready if session is ending due to /clear or /compact,
        # so the new session can find this parent and generate handoff summaries.
        # Claude Code session-end uses 'reason' field (not 'source').
        if session_id and self._session_storage:
            try:
                end_status = "expired"
                end_reason = event.data.get("reason")
                if end_reason in ("clear", "compact"):
                    end_status = "handoff_ready"
                # Don't downgrade handoff_ready → expired (PRE_COMPACT may have
                # already set handoff_ready before SESSION_END fires)
                if end_status == "expired":
                    current = self._session_storage.get(session_id)
                    if current and current.status == "handoff_ready":
                        end_status = "handoff_ready"
                self._session_storage.update_status(session_id, end_status)
            except Exception as e:
                self.logger.warning(f"Failed to update session status on end: {e}")

        return HookResponse(decision="allow")

    def _handle_pre_created_session(
        self,
        existing_session: Session,
        external_id: str,
        transcript_path: str | None,
        cli_source: str,
        event: HookEvent,
        cwd: str | None,
    ) -> HookResponse:
        """Handle session start for a pre-created session (terminal mode agent).

        Args:
            existing_session: Pre-created session object
            external_id: External (CLI-native) session ID
            transcript_path: Path to transcript file
            cli_source: CLI source (e.g., "claude-code")
            event: Hook event
            cwd: Current working directory

        Returns:
            HookResponse for the pre-created session
        """
        self.logger.info(f"Found pre-created session {external_id}, updating instead of creating")

        # Derive transcript path for CLIs that don't provide one natively
        if not transcript_path:
            input_data = event.data if event else {}
            transcript_path = self._derive_transcript_path(cli_source, input_data, external_id)

        # Update the session with actual runtime info
        if self._session_storage:
            self._session_storage.update(
                session_id=existing_session.id,
                jsonl_path=transcript_path,
                status="active",
            )

        # Cache mapping so subsequent hooks skip DB lookup
        if self._session_manager:
            self._session_manager.cache_session_mapping(
                external_id=external_id,
                source=cli_source,
                session_id=existing_session.id,
            )

        session_id = existing_session.id
        parent_session_id = existing_session.parent_session_id
        machine_id = self._get_machine_id()

        # Track registered session
        if transcript_path and self._session_coordinator:
            try:
                self._session_coordinator.register_session(external_id)
            except Exception as e:
                self.logger.error(f"Failed to setup session tracking: {e}")

        # Start the agent run if this is a terminal-mode agent session
        if existing_session.agent_run_id and self._session_coordinator:
            try:
                self._session_coordinator.start_agent_run(existing_session.agent_run_id)
            except Exception as e:
                self.logger.warning(f"Failed to start agent run: {e}")

        # Pipeline workflows are executed by the agent via run_pipeline MCP tool
        if existing_session.workflow_name and session_id:
            self.logger.debug(
                "Pipeline workflow registered for session — agent will execute via run_pipeline",
                extra={"workflow_name": existing_session.workflow_name, "session_id": session_id},
            )

        # Deep load default agent (rules, skills, variables) for pre-created session
        agent_result: AgentActivationResult | None = None
        input_data = event.data if event else {}
        try:
            agent_override = input_data.get("agent_name_override")
            agent_result = self._activate_default_agent(
                session_id,
                cli_source,
                existing_session.project_id,
                agent_name_override=agent_override,
            )
        except Exception as e:
            self.logger.error(
                f"Failed to activate default agent for pre-created session: {e}",
                exc_info=True,
            )

        # Update event metadata
        event.metadata["_platform_session_id"] = session_id

        # Register with Message Processor
        if self._message_processor and transcript_path:
            try:
                self._message_processor.register_session(
                    session_id, transcript_path, source=cli_source
                )
            except Exception as e:
                self.logger.warning(f"Failed to register with message processor: {e}")

        # Build additional context (agent AI context)
        additional_context: list[str] = []
        if agent_result and agent_result.context:
            additional_context.append(agent_result.context)

        return self._compose_session_response(
            session=existing_session,
            session_id=session_id,
            external_id=external_id,
            parent_session_id=parent_session_id,
            machine_id=machine_id,
            project_id=existing_session.project_id,
            task_id=event.task_id,
            additional_context=additional_context,
            is_pre_created=True,
            agent_info=agent_result,
        )

    def _resolve_agent_name(
        self,
        session_id: str,
        agent_name_override: str | None,
    ) -> str:
        """Determine which agent to activate.

        Priority: override param > existing _agent_type variable > ConfigStore default.

        Only called from _activate_default_agent after the session_storage None guard.
        """
        assert self._session_storage is not None
        if agent_name_override:
            return agent_name_override

        # Check if the session already has _agent_type set (e.g., from spawn_agent).
        # If so, use that instead of the global default — spawned agents should
        # keep the agent type assigned by the parent.
        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(self._session_storage.db)
        existing_vars = sv_mgr.get_variables(session_id)
        existing_agent_type = existing_vars.get("_agent_type") if existing_vars else None

        if existing_agent_type and existing_agent_type != "default":
            return str(existing_agent_type)

        from gobby.storage.config_store import ConfigStore

        config_store = ConfigStore(self._session_storage.db)
        return config_store.get("default_agent") or "default"

    def _build_agent_changes(
        self,
        agent_body: Any,
        session_id: str,
        enabled_rules: list[Any],
        all_skills: list[Any],
        enabled_variables: list[Any],
    ) -> tuple[dict[str, Any], set[str], set[str] | None]:
        """Build session variable changes from agent definition, rules, skills, and variables.

        Returns:
            (changes_dict, active_rule_names, active_skill_names)

        Only called from _activate_default_agent after the session_storage None guard.
        """
        assert self._session_storage is not None
        import json

        from gobby.workflows.selectors import (
            resolve_rules_for_agent,
            resolve_skills_for_agent,
            resolve_variables_for_agent,
        )

        active_rules = resolve_rules_for_agent(agent_body, enabled_rules)

        session = self._session_storage.get(session_id)
        is_spawned = bool(session and session.agent_run_id)

        changes: dict[str, Any] = {
            "_agent_type": agent_body.name,
            "_active_rule_names": list(active_rules),
            "is_spawned_agent": is_spawned,
        }

        active_skills = resolve_skills_for_agent(agent_body, all_skills)
        if active_skills is not None:
            changes["_active_skill_names"] = list(active_skills)

        if agent_body.workflows and agent_body.workflows.skill_format:
            changes["_skill_format"] = agent_body.workflows.skill_format

        if agent_body.workflows and agent_body.workflows.variables:
            for key, value in agent_body.workflows.variables.items():
                if key.startswith("_"):
                    self.logger.warning("Skipping reserved variable %r from agent definition", key)
                    continue
                changes[key] = value

        active_variable_names = resolve_variables_for_agent(agent_body, enabled_variables)
        for var_row in enabled_variables:
            if active_variable_names is None or var_row.name in active_variable_names:
                try:
                    var_body = json.loads(var_row.definition_json)
                    if var_row.name not in changes:
                        changes[var_row.name] = var_body.get("value")
                except json.JSONDecodeError:
                    self.logger.debug("Failed to parse variable definition for %s", var_row.name)

        return changes, active_rules, active_skills

    def _activate_default_agent(
        self,
        session_id: str,
        cli_source: str,
        project_id: str | None,
        agent_name_override: str | None = None,
    ) -> AgentActivationResult | None:
        """Activate the default agent for a session, merging its properties.

        Orchestrates: name resolution -> agent resolution -> changes building ->
        variable persistence -> context building.
        """
        if not self._session_manager or not self._session_storage:
            return None

        default_agent_name = self._resolve_agent_name(session_id, agent_name_override)
        if default_agent_name == "none":
            return None

        from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

        try:
            agent_body = resolve_agent(
                default_agent_name, self._session_storage.db, project_id=project_id
            )
        except AgentResolutionError as e:
            self.logger.error(f"Failed to resolve default agent '{default_agent_name}': {e}")
            return None

        if not agent_body:
            self.logger.debug(f"Default agent '{default_agent_name}' not found in DB")
            return None

        # Fetch rules, skills, and variables from DB
        from gobby.skills.manager import SkillManager
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        def_manager = LocalWorkflowDefinitionManager(self._session_storage.db)
        enabled_rules = [r for r in def_manager.list_all(workflow_type="rule") if r.enabled]
        enabled_variables = [v for v in def_manager.list_all(workflow_type="variable") if v.enabled]
        all_skills = SkillManager(self._session_storage.db).list_skills()

        # Build and persist session variables
        changes, active_rules, active_skills = self._build_agent_changes(
            agent_body, session_id, enabled_rules, all_skills, enabled_variables
        )

        from gobby.workflows.state_manager import SessionVariableManager

        SessionVariableManager(self._session_storage.db).merge_variables(session_id, changes)

        # Build injection context
        context_parts: list[str] = []
        preamble = agent_body.build_prompt_preamble()
        if preamble:
            context_parts.append(preamble)

        formatted, skills_count, injected_names = select_and_format_agent_skills(
            agent_body, all_skills, active_skills, cli_source
        )
        if formatted:
            context_parts.append(formatted)

        internal_keys = {
            "_agent_type",
            "_active_rule_names",
            "_active_skill_names",
            "_skill_format",
        }
        variables_count = len([k for k in changes if k not in internal_keys])

        return AgentActivationResult(
            context="\n\n".join(context_parts) if context_parts else None,
            agent_name=agent_body.name,
            description=agent_body.description,
            role=agent_body.role,
            goal=agent_body.goal,
            rules_count=len(active_rules),
            skills_count=skills_count,
            variables_count=variables_count,
            injected_skill_names=injected_names,
        )

    def _compose_session_response(
        self,
        session: Session | None,
        session_id: str | None,
        external_id: str,
        parent_session_id: str | None,
        machine_id: str,
        project_id: str | None = None,
        task_id: str | None = None,
        additional_context: list[str] | None = None,
        is_pre_created: bool = False,
        terminal_context: dict[str, Any] | None = None,
        agent_info: AgentActivationResult | None = None,
        session_source: str | None = None,
    ) -> HookResponse:
        """Build HookResponse for session start.

        Shared helper that builds the system message, context, and metadata
        for both pre-created and newly-created sessions.

        Args:
            session: Session object (used for seq_num)
            session_id: Session ID
            external_id: External (CLI-native) session ID
            parent_session_id: Parent session ID if any
            machine_id: Machine ID
            project_id: Project ID
            task_id: Task ID if any
            additional_context: Additional context strings to append (e.g., task/skill context)
            is_pre_created: Whether this is a pre-created session
            terminal_context: Terminal context dict to add to metadata
            session_source: Session source (e.g., "clear", "compact", "startup") for handoff indicator

        Returns:
            HookResponse with system_message, context, and metadata
        """
        # Build context_parts
        context_parts: list[str] = []
        if parent_session_id:
            context_parts.append(f"Parent session: {parent_session_id}")
        if additional_context:
            context_parts.extend(additional_context)

        # Compute session_ref from session object or fallback to session_id
        session_ref = session_id
        if session and session.seq_num:
            session_ref = f"#{session.seq_num}"

        # Build system message (terminal display)
        # Session ID: prefer #N, fallback to UUID only when no seq_num
        system_message = f"\nGobby Session ID: {session_ref}"
        system_message += " <- Use this for MCP tool calls (session_id parameter)"

        # Parent Session ID (before External ID, with handoff indicator)
        if parent_session_id and self._session_storage:
            try:
                parent = self._session_storage.get(parent_session_id)
                if parent:
                    parent_ref = f"#{parent.seq_num}" if parent.seq_num else parent_session_id
                    # Handoff indicator based on session_source
                    indicator = ""
                    if session_source == "clear":
                        indicator = " (Handoff)" if parent.summary_markdown else " (No Handoff)"
                    elif session_source == "compact":
                        indicator = " (Handoff)" if parent.compact_markdown else " (No Handoff)"
                    system_message += f"\nParent Session ID: {parent_ref}{indicator}"
                else:
                    system_message += f"\nParent Session ID: {parent_session_id}"
            except Exception:
                system_message += f"\nParent Session ID: {parent_session_id}"

        system_message += f"\nExternal ID: {external_id} (CLI-native, rarely needed)"

        # Agent info (only if agent loaded — absence signals activation failure)
        if agent_info:
            # Agent name only — description moves to tree node
            system_message += f"\nAgent: {agent_info.agent_name}"

            # Build tree nodes: description, role, goal, task, rules, variables, skills
            tree_nodes: list[str] = []
            if agent_info.description:
                tree_nodes.append(f"Description: {agent_info.description.strip()}")
            if agent_info.role:
                tree_nodes.append(f"Role: {agent_info.role.strip()}")
            if agent_info.goal:
                tree_nodes.append(f"Goal: {agent_info.goal.strip()}")
            # Current task (inside agent tree, with claimed/assigned indicator)
            if task_id and self._task_manager:
                try:
                    task = self._task_manager.get_task(task_id, project_id=project_id)
                    task_ref = f"#{task.seq_num}" if task and task.seq_num else task_id
                except Exception:
                    task_ref = task_id
                claim_status = "assigned" if parent_session_id else "claimed"
                tree_nodes.append(f"Current Task: {task_ref} ({claim_status})")
            tree_nodes.append(f"Rules: {agent_info.rules_count}")
            tree_nodes.append(f"Variables: {agent_info.variables_count}")
            # Skills is always last (may have sub-node)
            skills_label = f"Skills: {agent_info.skills_count}"

            for node in tree_nodes:
                system_message += f"\n├─ {node}"
            if agent_info.injected_skill_names:
                system_message += f"\n└─ {skills_label}"
                system_message += f"\n   └─ Injected: {', '.join(agent_info.injected_skill_names)}"
            else:
                system_message += f"\n└─ {skills_label}"

        # Build metadata
        metadata: dict[str, Any] = {
            "session_id": session_id,
            "session_ref": session_ref,
            "parent_session_id": parent_session_id,
            "machine_id": machine_id,
            "project_id": project_id,
            "external_id": external_id,
            "task_id": task_id,
        }
        if is_pre_created:
            metadata["is_pre_created"] = True
        if terminal_context:
            # Only include non-null terminal values
            for key, value in terminal_context.items():
                if value is not None:
                    metadata[f"terminal_{key}"] = value

        final_context = "\n".join(context_parts) if context_parts else None

        response = HookResponse(
            decision="allow",
            context=final_context,
            system_message=system_message,
            metadata=metadata,
        )
        self._apply_debug_echo(response)
        return response
