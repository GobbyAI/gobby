"""Session start event handler."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.event_handlers._session_responses import (
    build_claimed_task_context,
    compose_session_response,
    get_claimed_task_info,
)
from gobby.hooks.events import HookEvent, HookResponse
from gobby.tasks.state_semantics import get_claimed_session_id, is_active_claim_status
from gobby.workflows.summary_actions import schedule_tmux_window_rename

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.workflows.definitions import AgentDefinitionBody

_derive_logger = logging.getLogger(__name__)

SUMMARY_GENERATION_TIMEOUT_S = 120


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
    from gobby.skills.formatting import render_skills_for_context
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
    formatted = render_skills_for_context(selected)
    return formatted, len(selected), injected_names


class SessionStartMixin(EventHandlersBase):
    """Mixin for handling SESSION_START events and related helpers."""

    def _derive_transcript_path(
        self, cli_source: str, input_data: dict[str, Any], external_id: str
    ) -> str | None:
        """Derive transcript path for CLIs that don't provide one natively.

        Args:
            cli_source: CLI source name (gemini, codex, etc.)
            input_data: Hook event input data
            external_id: External session ID

        Returns:
            Path to transcript file, or None if not derivable.
        """
        if cli_source == "gemini":
            return self._find_gemini_transcript(input_data, external_id)
        if cli_source == "qwen":
            return self._find_qwen_transcript(input_data, external_id)
        return None

    def _find_gemini_transcript(self, input_data: dict[str, Any], external_id: str) -> str | None:
        """Locate a Gemini CLI JSON session transcript for the hook event."""
        return self._find_json_session_transcript("gemini", "Gemini", input_data, external_id)

    def _find_qwen_transcript(self, input_data: dict[str, Any], external_id: str) -> str | None:
        """Locate a Qwen CLI JSON session transcript for the hook event."""
        return self._find_json_session_transcript("qwen", "Qwen", input_data, external_id)

    def _find_json_session_transcript(
        self,
        cli_name: str,
        cli_label: str,
        input_data: dict[str, Any],
        external_id: str,
    ) -> str | None:
        """Find a JSON session transcript for supported CLIs (Gemini, Qwen)."""
        cwd = input_data.get("cwd")
        if not cwd:
            self.logger.debug("Cannot derive %s transcript: no cwd", cli_label)
            return None

        session_id = input_data.get("session_id") or external_id or ""
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = Path.home() / f".{cli_name}" / "tmp" / project_hash / "chats"

        if not chats_dir.exists():
            self.logger.debug("%s chats dir not found: %s", cli_label, chats_dir)
            return None

        # Try to match by session_id prefix (first 8 chars)
        prefix = session_id[:8] if session_id else ""
        if prefix:
            matches = sorted(chats_dir.glob(f"session-*-{prefix}.json"), reverse=True)
            if matches:
                self.logger.debug("Found %s transcript by prefix: %s", cli_label, matches[0])
                return str(matches[0])

        # Fallback: most recent session file
        all_sessions = sorted(chats_dir.glob("session-*.json"), reverse=True)
        if all_sessions:
            self.logger.debug("Found %s transcript (most recent): %s", cli_label, all_sessions[0])
            return str(all_sessions[0])

        self.logger.debug("No %s session files in %s", cli_label, chats_dir)
        return None

    def handle_session_start(self, event: HookEvent) -> HookResponse:
        """Handle SESSION_START event.

        Register session and execute session-handoff workflow.
        """
        _t0 = time.monotonic()
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

        _t_pre_check = time.monotonic()
        self.logger.debug(
            f"SESSION_START: cli={cli_source}, project={project_id}, source={session_source}"
        )

        # Step 0: Check if this is a pre-created session (terminal mode agent)
        # Two cases:
        # 1. Claude: We pass --session-id <internal_id>, so external_id IS our internal ID
        # 2. Terminal-backed CLIs: hook_dispatcher forwards GOBBY_SESSION_ID in terminal_context
        existing_session = None
        terminal_context = (
            input_data.get("terminal_context")
            if isinstance(input_data.get("terminal_context"), dict)
            else None
        )

        # Daemon-spawned ACP subprocesses (gemini --acp, qwen --acp) set
        # GOBBY_ACP_CHILD=1 and GOBBY_HOOKS_DISABLED=1 in their environment.
        # A current ghook short-circuits on GOBBY_HOOKS_DISABLED before ever
        # calling the daemon; this branch catches the stale-ghook case where
        # the binary isn't rebuilt yet but carries the GOBBY_ACP_CHILD marker
        # in terminal_context.
        if terminal_context and terminal_context.get("gobby_acp_child") == "1":
            self.logger.info(
                "Skipping session registration for ACP child process",
                extra={
                    "cli": cli_source,
                    "external_id": external_id,
                    "gobby_acp_child": terminal_context.get("gobby_acp_child"),
                },
            )
            return HookResponse()
        gobby_session_id_from_env = (
            terminal_context.get("gobby_session_id") if terminal_context else None
        )

        if self._session_manager:
            try:
                # Try to find by internal ID first (Claude case - external_id IS internal_id)
                existing_session = self._session_manager.get(external_id)
                if existing_session:
                    return self._handle_pre_created_session(
                        existing_session=existing_session,
                        external_id=external_id,
                        transcript_path=transcript_path,
                        cli_source=cli_source,
                        event=event,
                        cwd=cwd,
                        terminal_context=terminal_context,
                    )
            except Exception as e:
                self.logger.debug(f"No pre-created session found by external_id: {e}")

            # Terminal CLI case: look up the prepared child session by
            # GOBBY_SESSION_ID carried in terminal_context.
            if gobby_session_id_from_env and not existing_session:
                try:
                    existing_session = self._session_manager.get(gobby_session_id_from_env)
                    if existing_session:
                        self.logger.info(
                            f"Found pre-created session {gobby_session_id_from_env} via "
                            f"terminal_context, updating external_id to {external_id}"
                        )
                        # Update the session's external_id and terminal_context
                        # terminal_context contains parent_pid needed for kill_agent
                        self._session_manager.update(
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
                            terminal_context=terminal_context,
                        )
                except Exception as e:
                    self.logger.debug(f"No pre-created session found by gobby_session_id: {e}")

        _t_parent = time.monotonic()
        # Step 1: Find parent session
        # Check env vars first (spawned agent case), then handoff (source='clear')
        parent_session_id = input_data.get("parent_session_id")
        workflow_name = input_data.get("workflow_name")
        agent_depth = input_data.get("agent_depth")

        if (
            not parent_session_id
            and self._session_manager
            and session_source in ("clear", "compact")
        ):
            try:
                parent = self._session_manager.find_parent(
                    machine_id=machine_id,
                    project_id=project_id,
                    source=cli_source,
                    status="handoff_ready",
                )

                # Race condition: Claude Code fires session-start before session-end,
                # so the old session may still be active when we look for handoff_ready.
                # SESSION_END is fast (just sets status), so a short backoff suffices.
                if not parent and session_source in ("clear", "compact"):
                    deadline = time.monotonic() + 5  # 5s -- session_end is fast
                    while time.monotonic() < deadline:
                        time.sleep(0.3)
                        parent = self._session_manager.find_parent(
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

                    parent_vars = SessionVariableManager(self._session_manager.db).get_variables(
                        parent.id
                    )
                    handoff_source = parent_vars.get("handoff_source")
                    if handoff_source in ("clear", "compact"):
                        session_source = handoff_source
                        input_data["source"] = session_source
            except Exception as e:
                self.logger.warning(f"Error finding parent session: {e}")

        _t_register = time.monotonic()
        # Step 2: Register new session with parent if found
        # terminal_context already extracted in Step 0
        # Parse agent_depth as int if provided
        agent_depth_val = 0
        if agent_depth:
            try:
                agent_depth_val = int(agent_depth)
            except (ValueError, TypeError):
                pass
        sandbox_enabled = input_data.get("sandbox_enabled")
        sandbox_enabled_val = sandbox_enabled if isinstance(sandbox_enabled, bool) else None

        session_id = None
        if self._session_manager:
            session_id = self._session_manager.register_session(
                external_id=external_id,
                machine_id=machine_id,
                project_id=project_id,
                parent_session_id=parent_session_id,
                transcript_path=transcript_path,
                source=cli_source,
                project_path=cwd,
                terminal_context=terminal_context,
                workflow_name=workflow_name,
                agent_depth=agent_depth_val,
                sandbox_enabled=sandbox_enabled_val,
            )

        # Step 2b: Mark parent session as expired after successful handoff
        if parent_session_id and self._session_manager:
            try:
                self._session_manager.mark_session_expired(parent_session_id)
                self.logger.debug(f"Marked parent session {parent_session_id} as expired")
            except Exception as e:
                self.logger.warning(f"Failed to mark parent session as expired: {e}")

        # Step 2c: Set code_index_available if project has an index
        self._setup_code_index(session_id, project_id)

        # Step 2d: Pipeline workflows are executed by the agent via run_pipeline MCP tool.
        # Agent rules enforce pipeline execution by blocking all tools except
        # progressive discovery and run_pipeline.
        if workflow_name and session_id:
            self.logger.debug(
                "Pipeline workflow registered for session -- agent will execute via run_pipeline",
                extra={"workflow_name": workflow_name, "session_id": session_id},
            )

        _t_activate = time.monotonic()
        # Step 2e: Deep load default agent (rules, skills, variables) for new session
        agent_result: AgentActivationResult | None = None
        if session_id and not input_data.get("skip_default_agent_activation"):
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

        _t_track = time.monotonic()
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

        _t_msg_proc = time.monotonic()
        # Step 5: Register with Message Processor
        if self._message_processor and transcript_path and session_id:
            try:
                self._message_processor.register_session(
                    session_id, transcript_path, source=cli_source
                )
            except Exception as e:
                self.logger.warning(f"Failed to register session with message processor: {e}")

        _t_handoff = time.monotonic()
        # Build additional context (agent AI context + task context)
        additional_context: list[str] = []
        if agent_result and agent_result.context:
            additional_context.append(agent_result.context)

        # Populate handoff session variables for inject_context rule templates
        if parent_session_id and session_id and self._session_manager:
            from gobby.workflows.state_manager import SessionVariableManager

            sv_mgr = SessionVariableManager(self._session_manager.db)
            current_vars = sv_mgr.get_variables(session_id)
            if current_vars.get("auto_inject_handoff", True):
                parent = self._session_manager.get(parent_session_id)
                if parent:
                    # For /clear: summary generation was kicked off by
                    # BEFORE_AGENT (fire-and-forget). Poll until it arrives.
                    # For /compact: PRE_COMPACT already kicked it off.
                    # Both paths wait for the summary to be generated.
                    needs_wait = not parent.summary_markdown
                    if needs_wait:
                        # Ensure generation is started (idempotent if already running)
                        summary_event = threading.Event()
                        max_wait_s = SUMMARY_GENERATION_TIMEOUT_S
                        dispatched = False
                        if self._dispatch_session_summaries_fn:
                            try:
                                self._dispatch_session_summaries_fn(
                                    parent_session_id, True, summary_event
                                )
                                dispatched = True
                            except Exception as e:
                                self.logger.warning(
                                    f"Failed to dispatch session summaries "
                                    f"for parent {parent_session_id}: {e}"
                                )

                        # Wait for summary generation to complete
                        if dispatched:
                            if summary_event.wait(timeout=max_wait_s):
                                self.logger.debug(
                                    f"Session summary signaled for parent {parent_session_id}",
                                )
                            else:
                                self.logger.warning(
                                    f"Timed out waiting for session summary for parent {parent_session_id} after {max_wait_s:.0f}s",
                                )
                        # Re-read parent after generation
                        parent = self._session_manager.get(parent_session_id)

                    handoff_vars: dict[str, Any] = {}
                    if parent and parent.summary_markdown:
                        handoff_vars["session_summary"] = parent.summary_markdown
                        # Also set full_session_summary (used by inject-previous-session-summary rule)
                        handoff_vars["full_session_summary"] = parent.summary_markdown
                    if handoff_vars:
                        sv_mgr.merge_variables(session_id, handoff_vars)

                    # Preserve task claim state across compaction/clear
                    parent_vars = sv_mgr.get_variables(parent_session_id)
                    if session_source in ("compact", "clear"):
                        _TASK_CLAIM_KEYS = (
                            "task_claimed",
                            "claimed_tasks",
                            "session_had_task",
                        )
                        task_handoff = {
                            k: parent_vars[k] for k in _TASK_CLAIM_KEYS if parent_vars.get(k)
                        }
                        if task_handoff:
                            claimed_tasks = task_handoff.get("claimed_tasks") or {}
                            merged_claims: dict[str, Any] = {}
                            if task_handoff.get("session_had_task"):
                                merged_claims["session_had_task"] = True

                            filtered_claims: dict[str, str] = {}
                            if task_handoff.get("task_claimed") and claimed_tasks:
                                for claimed_id, claimed_ref in claimed_tasks.items():
                                    task_obj = None
                                    if self._task_manager:
                                        try:
                                            task_obj = self._task_manager.get_task(claimed_id)
                                        except Exception as e:
                                            self.logger.debug(
                                                f"Best-effort task lookup failed for session={session_id} task={claimed_id}: {e}"
                                            )
                                            continue

                                    if task_obj is not None:
                                        if not is_active_claim_status(task_obj.status):
                                            continue
                                        current_owner = get_claimed_session_id(task_obj)
                                        if current_owner not in (None, parent_session_id):
                                            self.logger.debug(
                                                "Skipping task handoff for session=%s task=%s; "
                                                "already assigned to %s",
                                                session_id,
                                                claimed_id,
                                                current_owner,
                                            )
                                            continue
                                        if self._task_manager is None:
                                            continue
                                        try:
                                            self._task_manager.claim_task(
                                                claimed_id,
                                                session_id=session_id,
                                                force=current_owner == parent_session_id,
                                            )
                                        except Exception as e:
                                            self.logger.debug(
                                                f"Best-effort task re-assignment failed for session={session_id} task={claimed_id}: {e}"
                                            )
                                            continue

                                    filtered_claims[claimed_id] = claimed_ref
                                    if self._session_task_manager:
                                        try:
                                            self._session_task_manager.link_task(
                                                session_id, claimed_id, "claimed"
                                            )
                                        except Exception as e:
                                            self.logger.debug(
                                                f"Best-effort session-task link failed for session={session_id} task={claimed_id}: {e}"
                                            )
                            if filtered_claims:
                                merged_claims["task_claimed"] = True
                                merged_claims["claimed_tasks"] = filtered_claims
                            if merged_claims:
                                sv_mgr.merge_variables(session_id, merged_claims)

        # Deterministic claimed task context injection (compact/clear/restart)
        # Ensures agents always know which tasks they've claimed, even after
        # context is wiped by /compact, /clear, autocompact, or daemon restart.
        if session_id and project_id and not event.task_id:
            claimed_ctx = build_claimed_task_context(self, session_id, project_id)
            if claimed_ctx:
                additional_context.append(claimed_ctx)

        # Populate task_context session variable for inject_context rule templates
        if event.task_id and session_id and self._session_manager:
            task_title = event.metadata.get("_task_title", "Unknown Task")
            task_context_str = f"You are working on task: {task_title} ({event.task_id})"
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(self._session_manager.db).merge_variables(
                session_id, {"task_context": task_context_str}
            )

        if event.task_id:
            task_title = event.metadata.get("_task_title", "Unknown Task")
            additional_context.append("\n## Active Task Context\n")
            additional_context.append(f"You are working on task: {task_title} ({event.task_id})")

        # Fetch session to get seq_num for #N display
        session_obj = None
        if session_id and self._session_manager:
            session_obj = self._session_manager.get(session_id)

        # Fetch claimed task info for system_message tree display
        claimed_tasks_info = get_claimed_task_info(self, session_id, project_id)

        def _ms(a: float, b: float) -> int:
            return int((b - a) * 1000)

        _t_end = time.monotonic()
        self.logger.info(
            f"SESSION_START timing [{session_source}]: pre_check={_ms(_t0, _t_pre_check)}ms parent={_ms(_t_pre_check, _t_parent)}ms register={_ms(_t_parent, _t_register)}ms activate_agent={_ms(_t_register, _t_activate)}ms track={_ms(_t_activate, _t_track)}ms msg_proc={_ms(_t_track, _t_msg_proc)}ms handoff={_ms(_t_msg_proc, _t_handoff)}ms total={_ms(_t0, _t_end)}ms",
        )

        return compose_session_response(
            self,
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
            claimed_tasks_info=claimed_tasks_info,
        )

    def _handle_pre_created_session(
        self,
        existing_session: Session,
        external_id: str,
        transcript_path: str | None,
        cli_source: str,
        event: HookEvent,
        cwd: str | None,
        terminal_context: dict[str, Any] | None = None,
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
        session_obj = existing_session
        if self._session_manager:
            updated = self._session_manager.update(
                session_id=existing_session.id,
                transcript_path=transcript_path,
                status="active",
            )
            if updated is not None:
                session_obj = updated

        tmux_pane_added = False
        if self._session_manager and terminal_context:
            refreshed, tmux_pane_added = self._session_manager.backfill_terminal_context(
                existing_session.id,
                terminal_context,
            )
            if refreshed is not None:
                session_obj = refreshed

        if tmux_pane_added:
            title = getattr(session_obj, "title", None)
            digest = getattr(session_obj, "digest_markdown", None)
            if title and digest:
                schedule_tmux_window_rename(
                    session_obj,
                    title,
                    loop=getattr(self._session_coordinator, "_event_loop", None),
                )

        # Cache mapping so subsequent hooks skip DB lookup
        if self._session_manager:
            self._session_manager.cache_session_mapping(
                external_id=external_id,
                source=cli_source,
                session_id=existing_session.id,
            )

        session_id = session_obj.id
        parent_session_id = session_obj.parent_session_id
        machine_id = self._get_machine_id()

        # Track registered session
        if transcript_path and self._session_coordinator:
            try:
                self._session_coordinator.register_session(external_id)
            except Exception as e:
                self.logger.error(f"Failed to setup session tracking: {e}")

        # Start the agent run if this is a terminal-mode agent session
        if session_obj.agent_run_id and self._session_coordinator:
            try:
                self._session_coordinator.start_agent_run(session_obj.agent_run_id)
            except Exception as e:
                self.logger.warning(f"Failed to start agent run: {e}")

        # Pipeline workflows are executed by the agent via run_pipeline MCP tool
        if session_obj.workflow_name and session_id:
            self.logger.debug(
                "Pipeline workflow registered for session -- agent will execute via run_pipeline",
                extra={"workflow_name": session_obj.workflow_name, "session_id": session_id},
            )

        # Set code_index_available if project has an index
        self._setup_code_index(session_id, session_obj.project_id)

        # Deep load default agent (rules, skills, variables) for pre-created session
        agent_result: AgentActivationResult | None = None
        input_data = event.data if event else {}
        try:
            agent_override = input_data.get("agent_name_override")
            agent_result = self._activate_default_agent(
                session_id,
                cli_source,
                session_obj.project_id,
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

        # Deterministic claimed task context injection for pre-created sessions
        if session_id and session_obj.project_id and not event.task_id:
            claimed_ctx = build_claimed_task_context(self, session_id, session_obj.project_id)
            if claimed_ctx:
                additional_context.append(claimed_ctx)

        # Fetch claimed task info for system_message tree display
        claimed_tasks_info = get_claimed_task_info(self, session_id, session_obj.project_id)

        return compose_session_response(
            self,
            session=session_obj,
            session_id=session_id,
            external_id=external_id,
            parent_session_id=parent_session_id,
            machine_id=machine_id,
            project_id=session_obj.project_id,
            task_id=event.task_id,
            additional_context=additional_context,
            is_pre_created=True,
            terminal_context=session_obj.terminal_context,
            agent_info=agent_result,
            claimed_tasks_info=claimed_tasks_info,
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
        if self._session_manager is None:
            raise RuntimeError("session storage is not initialized")
        if agent_name_override:
            return agent_name_override

        # Check if the session already has _agent_type set (e.g., from spawn_agent).
        # If so, use that instead of the global default -- spawned agents should
        # keep the agent type assigned by the parent.
        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(self._session_manager.db)
        existing_vars = sv_mgr.get_variables(session_id)
        existing_agent_type = existing_vars.get("_agent_type") if existing_vars else None

        if existing_agent_type and existing_agent_type != "default":
            return str(existing_agent_type)

        from gobby.storage.config_store import ConfigStore

        config_store = ConfigStore(self._session_manager.db)
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
        if self._session_manager is None:
            raise RuntimeError("session storage is not initialized")

        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        session = self._session_manager.get(session_id)
        is_spawned = bool(session and session.agent_run_id)

        return build_persona_changes(
            agent_body=agent_body,
            session_id=session_id,
            db=self._session_manager.db,
            enabled_rules=enabled_rules,
            all_skills=all_skills,
            enabled_variables=enabled_variables,
            is_spawned=is_spawned,
        )

    def _setup_code_index(self, session_id: str | None, project_id: str | None) -> None:
        """Set code_index_available session variable if the project has an index."""
        if not session_id or not project_id or not self._session_manager:
            return
        try:
            from gobby.code_index.storage import CodeIndexStorage
            from gobby.workflows.state_manager import SessionVariableManager

            cis = CodeIndexStorage(self._session_manager.db)
            stats = cis.get_project_stats(project_id)
            if stats and stats.total_symbols > 0:
                sv_mgr = SessionVariableManager(self._session_manager.db)
                sv_mgr.set_variable(session_id, "code_index_available", True)
        except Exception as e:
            self.logger.debug(f"Could not check code index availability: {e}")

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
        if self._session_manager is None:
            return None

        _ta0 = time.monotonic()
        default_agent_name = self._resolve_agent_name(session_id, agent_name_override)
        if default_agent_name == "none":
            return None

        _ta_resolve = time.monotonic()
        from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

        try:
            agent_body = resolve_agent(
                default_agent_name, self._session_manager.db, project_id=project_id
            )
        except AgentResolutionError as e:
            self.logger.error(f"Failed to resolve default agent '{default_agent_name}': {e}")
            return None

        if not agent_body:
            self.logger.debug(f"Default agent '{default_agent_name}' not found in DB")
            return None

        # Fetch rules, skills, and variables from DB
        _ta_queries = time.monotonic()
        from gobby.skills.manager import SkillManager
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        def_manager = LocalWorkflowDefinitionManager(self._session_manager.db)
        enabled_rules = [r for r in def_manager.list_all(workflow_type="rule") if r.enabled]
        enabled_variables = [v for v in def_manager.list_all(workflow_type="variable") if v.enabled]
        all_skills = SkillManager(self._session_manager.db).list_skills()

        # Build and persist session variables
        _ta_build = time.monotonic()
        changes, active_rules, active_skills = self._build_agent_changes(
            agent_body, session_id, enabled_rules, all_skills, enabled_variables
        )

        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(self._session_manager.db)

        # Compute variables_count BEFORE the persistence filter so it reflects
        # the true number of user-facing variables for the agent, not the
        # persistence delta (which is artificially small because get_variables()
        # merges definition defaults, making 'existing' truthy even on fresh sessions).
        internal_keys = {
            "_agent_type",
            "_active_rule_names",
            "_active_skill_names",
            "_skill_format",
            "_agent_blocked_tools",
            "_agent_blocked_mcp_tools",
            "is_spawned_agent",
        }
        variables_count = len([k for k in changes if k not in internal_keys])

        # Don't overwrite existing session variables with defaults on compact/restart.
        # Internal keys reflect current agent config and are always re-applied.
        # User-facing variables (errors_resolved, stop_attempts, etc.)
        # are only set if not already present -- preserving agent-set values.
        existing = sv_mgr.get_variables(session_id)
        if existing:
            _ALWAYS_REAPPLY = {
                "_agent_type",
                "_active_rule_names",
                "_active_skill_names",
                "_skill_format",
                "_agent_blocked_tools",
                "_agent_blocked_mcp_tools",
                "is_spawned_agent",
            }
            changes = {
                k: v for k, v in changes.items() if k in _ALWAYS_REAPPLY or k not in existing
            }

        _ta_vars = time.monotonic()
        sv_mgr.merge_variables(session_id, changes)

        # Build injection context — identity only (role + personality).
        # Instructions, skills, and code-index are deferred to first before_agent.
        _ta_format = time.monotonic()
        identity_parts: list[str] = []
        if agent_body.role:
            identity_parts.append(f"## Role\n{agent_body.role}")
        if agent_body.personality:
            identity_parts.append(f"## Personality\n{agent_body.personality}")

        # Skills count is no longer needed at SessionStart (agent tree removed).
        # Selection + formatting deferred to _inject_agent_instructions_if_needed().
        skills_count = 0
        injected_names: list[str] = []

        def _ms(a: float, b: float) -> int:
            return int((b - a) * 1000)

        _ta_end = time.monotonic()
        self.logger.info(
            f"_activate_default_agent timing: resolve_name={_ms(_ta0, _ta_resolve)}ms resolve_agent={_ms(_ta_resolve, _ta_queries)}ms db_queries={_ms(_ta_queries, _ta_build)}ms build_changes={_ms(_ta_build, _ta_vars)}ms merge_vars={_ms(_ta_vars, _ta_format)}ms format={_ms(_ta_format, _ta_end)}ms total={_ms(_ta0, _ta_end)}ms",
        )

        return AgentActivationResult(
            context="\n\n".join(identity_parts) if identity_parts else None,
            agent_name=agent_body.name,
            description=agent_body.description,
            role=agent_body.role,
            goal=agent_body.goal,
            rules_count=len(active_rules),
            skills_count=skills_count,
            variables_count=variables_count,
            injected_skill_names=injected_names,
        )

    # ------------------------------------------------------------------
    # Thin delegation methods so that tests (and any external code) can
    # still call self._get_claimed_task_info() etc. as instance methods.
    # The real logic lives in _session_responses.py as module functions.
    # ------------------------------------------------------------------

    def _get_claimed_task_info(
        self,
        session_id: str | None,
        project_id: str | None,
    ) -> list[tuple[str, str, str]] | None:
        """Delegate to module-level get_claimed_task_info."""
        return get_claimed_task_info(self, session_id, project_id)

    def _build_claimed_task_context(
        self,
        session_id: str,
        project_id: str | None,
    ) -> str | None:
        """Delegate to module-level build_claimed_task_context."""
        return build_claimed_task_context(self, session_id, project_id)

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
        claimed_tasks_info: list[tuple[str, str, str]] | None = None,
    ) -> HookResponse:
        """Delegate to module-level compose_session_response."""
        return compose_session_response(
            self,
            session=session,
            session_id=session_id,
            external_id=external_id,
            parent_session_id=parent_session_id,
            machine_id=machine_id,
            project_id=project_id,
            task_id=task_id,
            additional_context=additional_context,
            is_pre_created=is_pre_created,
            terminal_context=terminal_context,
            agent_info=agent_info,
            session_source=session_source,
            claimed_tasks_info=claimed_tasks_info,
        )
