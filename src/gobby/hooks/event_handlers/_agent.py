from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse, SessionSource

logger = logging.getLogger(__name__)

# Pattern for /gobby or /gobby skillname with optional args
_GOBBY_CMD_PATTERN = re.compile(r"^/gobby(?::(\S+))?\s*(.*)?$", re.IGNORECASE | re.DOTALL)


def _load_agent_prompt(
    name: str,
    context: dict[str, Any] | None = None,
    fallback: str = "",
) -> str:
    """Load an agent prompt from bundled files, render if templated.

    Falls back to the hardcoded string if the file is missing (e.g.,
    editable install without the prompts directory).
    """
    from gobby.prompts.sync import get_bundled_prompts_path

    prompt_file = get_bundled_prompts_path() / "agent" / f"{name}.md"
    if not prompt_file.exists():
        return fallback

    try:
        raw = prompt_file.read_text(encoding="utf-8")
        # Strip YAML frontmatter
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                content = parts[2].strip()
            else:
                content = raw.strip()
        else:
            content = raw.strip()

        # Render Jinja2 templates if context provided
        if context and "{{" in content:
            from gobby.workflows.templates import TemplateEngine

            return TemplateEngine().render(content, context)
        return content
    except Exception:
        logger.debug(f"Failed to load agent prompt {name}, using fallback", exc_info=True)
        return fallback


class AgentEventHandlerMixin(EventHandlersBase):
    """Mixin for handling agent-related events."""

    def handle_before_agent(self, event: HookEvent) -> HookResponse:
        """Handle BEFORE_AGENT event (user prompt submit)."""
        input_data = event.data
        prompt = input_data.get("prompt", "")
        session_id = event.metadata.get("_platform_session_id")

        context_parts = []

        if session_id:
            self.logger.debug(f"BEFORE_AGENT: session {session_id}, prompt_len={len(prompt)}")

            # Update status to active (unless /clear or /exit)
            prompt_lower = prompt.strip().lower()
            if prompt_lower not in ("/clear", "/exit") and self._session_manager:
                try:
                    self._session_manager.update_session_status(session_id, "active")
                    if self._session_manager:
                        self._session_manager.reset_transcript_processed(session_id)
                except Exception as e:
                    self.logger.warning(f"Failed to update session status: {e}")

            # Handle /clear command - generate boundary summaries before clear/exit
            # and set handoff_source so session-end marks the session handoff_ready.
            if prompt_lower in ("/clear", "/exit"):
                self.logger.debug(f"Detected {prompt_lower} - generating session summaries")
                try:
                    if self._dispatch_session_summaries_fn:
                        self._dispatch_session_summaries_fn(session_id, False, None)
                except Exception as e:
                    self.logger.warning(
                        f"Failed to generate session summaries on {prompt_lower}: {e}"
                    )
                # Belt-and-suspenders: set handoff_source directly in addition to
                # the prepare-clear-handoff rule, so session-end marks handoff_ready
                # even if the rule engine is slow or disabled.
                if self._session_manager:
                    try:
                        from gobby.workflows.state_manager import SessionVariableManager

                        sv_mgr = SessionVariableManager(self._session_manager.db)
                        sv_mgr.set_variable(session_id, "handoff_source", prompt_lower.lstrip("/"))
                    except Exception as e:
                        self.logger.warning(f"Failed to set handoff_source: {e}")

        # Skill interception — runs before lifecycle workflows
        if self._skill_manager and prompt.strip():
            try:
                skill_context = self._intercept_skill_command(prompt.strip(), session_id)
                if skill_context:
                    context_parts.append(skill_context)
                else:
                    # Try trigger-based suggestion for non-command prompts
                    suggestion = self._suggest_skills(prompt.strip())
                    if suggestion:
                        context_parts.append(suggestion)
            except Exception as e:
                self.logger.error(f"Failed skill interception: {e}", exc_info=True)

        response = HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )

        # Inject agent instructions (instructions block + skills + code-index)
        # on first before_agent. Identity (role + personality) was injected at
        # SessionStart; this defers the heavier instructional content.
        if session_id:
            try:
                self._inject_agent_instructions_if_needed(event, session_id, response)
            except Exception as e:
                self.logger.error(f"Failed to inject agent instructions: {e}", exc_info=True)

        self._apply_debug_echo(response)
        return response

    def _inject_agent_instructions_if_needed(
        self, event: HookEvent, session_id: str, response: HookResponse
    ) -> None:
        """Format and inject agent instructions on first before_agent.

        Everything needed is already in DB from SessionStart activation:
        - Agent name: _agent_type session variable
        - Active skills: _active_skill_names session variable
        - Agent definition: workflow_definitions table
        """
        if not self._session_manager:
            return

        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(self._session_manager.db)
        variables = sv_mgr.get_variables(session_id)

        identity_reinject = bool(variables.get("_agent_identity_reinject"))
        if variables.get("_agent_context_injected") and not identity_reinject:
            return

        agent_name = variables.get("_agent_type", "default")
        active_skills_raw = variables.get("_active_skill_names")
        active_skills = set(active_skills_raw) if active_skills_raw is not None else None
        cli_source = event.source.value

        # Get project_id for project-specific agent resolution
        project_id = None
        try:
            session_row = self._session_manager.get(session_id)
            if session_row:
                project_id = session_row.project_id
        except Exception as e:
            self.logger.debug(
                "Failed to resolve session %s while injecting agent context: %s",
                session_id,
                e,
                exc_info=True,
            )

        from gobby.workflows.agent_resolver import resolve_agent

        agent_body = resolve_agent(agent_name, self._session_manager.db, project_id=project_id)
        if not agent_body:
            return

        parts: list[str] = []

        if identity_reinject:
            if agent_body.role:
                parts.append(f"## Role\n{agent_body.role}")
            if agent_body.goal:
                parts.append(f"## Goal\n{agent_body.goal}")
            if agent_body.personality:
                parts.append(f"## Personality\n{agent_body.personality}")

        if agent_body.instructions:
            parts.append(f"## Instructions\n{agent_body.instructions}")

        # Format skills list
        from gobby.hooks.event_handlers._session_start import select_and_format_agent_skills
        from gobby.skills.manager import SkillManager

        all_skills = SkillManager(self._session_manager.db).list_skills()
        formatted, _, _ = select_and_format_agent_skills(
            agent_body, all_skills, active_skills, cli_source
        )
        if formatted:
            parts.append(formatted)

        if parts:
            instructions_context = "\n\n".join(parts)
            if response.context:
                response.context = f"{instructions_context}\n\n{response.context}"
            else:
                response.context = instructions_context

        sv_mgr.merge_variables(
            session_id,
            {
                "_agent_context_injected": True,
                "_agent_identity_reinject": False,
            },
        )

    def _intercept_skill_command(self, prompt: str, session_id: str | None = None) -> str | None:
        """Intercept /gobby and /gobby skillname commands.

        Returns context string to inject, or None if not a /gobby command.
        Supports space syntax (/gobby expand) and legacy colon syntax.
        """
        match = _GOBBY_CMD_PATTERN.match(prompt)
        if not match:
            return None

        skill_name = match.group(1)  # None for bare /gobby or space syntax
        args = (match.group(2) or "").strip()

        # Support space syntax: /gobby expand → treat first word of args as skill name
        # Also supports /gobby skill(s) <name> as a namespace prefix
        resolved = None
        if not skill_name and args and self._skill_manager:
            parts = args.split(None, 1)
            first_word = parts[0]
            if first_word.lower() in ("skill", "skills"):
                # /gobby skill(s) <name> → shift to second word
                if len(parts) > 1:
                    sub_parts = parts[1].split(None, 1)
                    skill_name = sub_parts[0]
                    args = sub_parts[1] if len(sub_parts) > 1 else ""
                    resolved = self._skill_manager.resolve_skill_name(skill_name)
                # bare /gobby skills → fall through to help
            elif first_word.lower() != "help":
                skill_name = first_word
                resolved = self._skill_manager.resolve_skill_name(first_word)
                if resolved:
                    args = parts[1] if len(parts) > 1 else ""

        # /gobby or /gobby help → generate help
        if not skill_name or skill_name.lower() == "help":
            return self._generate_help_content(session_id)

        # /gobby skillname → resolve and inject
        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        skill = resolved if resolved else self._skill_manager.resolve_skill_name(skill_name)

        if not skill:
            return self._skill_not_found_context(skill_name)

        # Wrap skill content in context tags
        parts = [f'<skill-context name="{skill.name}">']
        parts.append(skill.content)
        parts.append("</skill-context>")

        if args:
            parts.append(f"\nUser arguments: {args}")

        return "\n".join(parts)

    def _suggest_skills(self, prompt: str) -> str | None:
        """Suggest skills based on trigger keyword matching.

        Only runs for non-slash-command prompts. Returns a lightweight hint
        if a strong match is found (score >= 0.7).
        """
        # Skip if it looks like a slash command
        if prompt.startswith("/"):
            return None

        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        matches = self._skill_manager.match_triggers(prompt, threshold=0.7)

        if not matches:
            return None

        skill, score = matches[0]
        fallback = f'Relevant skill available: `get_skill(name="{skill.name}")` on `gobby-skills`'
        return _load_agent_prompt("skill-hint", {"skill_name": skill.name}, fallback)

    def _generate_help_content(self, session_id: str | None = None) -> str:
        """Generate help content listing all available skills."""
        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        skills = self._skill_manager.discover_core_skills()

        if session_id and self._session_manager:
            try:
                from gobby.workflows.state_manager import SessionVariableManager

                sv_mgr = SessionVariableManager(self._session_manager.db)
                sv = sv_mgr.get_variables(session_id)
                if sv:
                    active_names = sv.get("_active_skill_names")
                    if active_names is not None:
                        active_set = set(active_names)
                        skills = [s for s in skills if s.name in active_set]
            except Exception:
                pass

        # Sort alphabetically, skip always-apply skills (they're auto-injected)
        user_skills = sorted(
            [s for s in skills if not s.is_always_apply()],
            key=lambda s: s.name,
        )

        skill_lines = []
        for skill in user_skills:
            desc = skill.description.split(".")[0] if skill.description else ""
            skill_lines.append(f"- `/gobby {skill.name}` — {desc}")
        skills_list = "\n".join(skill_lines)

        fallback = (
            "# Gobby Skills\n\n"
            "Invoke skills directly with `/gobby skillname` syntax:\n\n"
            f"{skills_list}\n\n"
            "**MCP access**: `list_skills()` / `get_skill(name)` on `gobby-skills`.\n"
            "**Hub search**: `search_hub(query)` on `gobby-skills`.\n"
            "**MCP tools**: `list_mcp_servers()` for tool discovery."
        )

        return _load_agent_prompt("help-content", {"skills_list": skills_list}, fallback)

    def _skill_not_found_context(self, name: str) -> str:
        """Generate context for an unrecognized skill name."""
        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        skills = self._skill_manager.discover_core_skills()

        # Find close matches (name contains or starts with input)
        name_lower = name.lower()
        close = sorted(
            s.name
            for s in skills
            if not s.is_always_apply()
            and (name_lower in s.name.lower() or s.name.lower().startswith(name_lower))
        )[:5]

        # Build fallback
        lines = [f"Skill '{name}' not found."]
        if close:
            lines.append("")
            lines.append("Did you mean:")
            for match in close:
                lines.append(f"  - `/gobby {match}`")
        lines.extend(["", "Run `/gobby` or `/gobby help` to see all available skills."])
        fallback = "\n".join(lines)

        return _load_agent_prompt(
            "skill-not-found",
            {"skill_name": name, "close_matches": close},
            fallback,
        )

    def handle_after_agent(self, event: HookEvent) -> HookResponse:
        """Handle AFTER_AGENT event."""
        session_id = event.metadata.get("_platform_session_id")
        cli_source = event.source.value

        context_parts: list[str] = []

        if session_id:
            self.logger.debug(f"AFTER_AGENT: session {session_id}, cli={cli_source}")
            if self._session_manager:
                try:
                    self._session_manager.update_session_status(session_id, "paused")
                except Exception as e:
                    self.logger.warning(f"Failed to update session status: {e}")
        else:
            self.logger.debug(f"AFTER_AGENT: cli={cli_source}")

        return HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )

    def handle_stop(self, event: HookEvent) -> HookResponse:
        """Handle STOP event (Claude Code only)."""
        session_id = event.metadata.get("_platform_session_id")

        context_parts: list[str] = []

        if session_id:
            self.logger.debug(f"STOP: session {session_id}")
            if self._session_manager:
                try:
                    self._session_manager.update_session_status(session_id, "paused")
                except Exception as e:
                    self.logger.warning(f"Failed to update session status: {e}")
        else:
            self.logger.debug("STOP")

        response = HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )
        self._apply_debug_echo(response)
        return response

    def handle_pre_compact(self, event: HookEvent) -> HookResponse:
        """Handle PRE_COMPACT event.

        Note: Gemini- and Qwen-compatible CLIs fire PreCompress constantly
        during normal operation, unlike Claude which fires it only when
        approaching context limits. We skip handoff logic and workflow
        execution for those CLIs to avoid excessive state changes and
        workflow interruptions.
        """
        trigger = event.data.get("trigger", "auto")
        session_id = event.metadata.get("_platform_session_id")

        # Skip handoff logic for Gemini/Qwen - they fire PreCompress too frequently
        if event.source in {SessionSource.GEMINI, SessionSource.QWEN}:
            self.logger.debug(
                f"PRE_COMPACT ({trigger}): session {session_id} "
                f"[{event.source.value.title()} - skipped]"
            )
            return HookResponse(decision="allow")

        if session_id:
            self.logger.debug(f"PRE_COMPACT ({trigger}): session {session_id}")
            # Mark session as handoff_ready so it can be found as parent after compact
            if self._session_manager:
                self._session_manager.update_session_status(session_id, "handoff_ready")
            # Generate session summaries from digest before compaction
            try:
                if self._dispatch_session_summaries_fn:
                    self._dispatch_session_summaries_fn(session_id, False, None)
            except Exception as e:
                self.logger.warning(f"Failed to generate session summaries on compact: {e}")
        else:
            self.logger.debug(f"PRE_COMPACT ({trigger})")

        return HookResponse(decision="allow")

    def handle_subagent_start(self, event: HookEvent) -> HookResponse:
        """Handle SUBAGENT_START event.

        Marks the subagent's session with correct agent_depth so that
        lifecycle processing can skip LLM-heavy steps for subagents.
        Also sets is_subagent=True so rule engine unblocks native task
        tools and blocks gobby-tasks for the duration of the subagent.
        """
        input_data = event.data
        session_id = event.metadata.get("_platform_session_id")
        agent_id = input_data.get("agent_id")
        subagent_id = input_data.get("subagent_id")

        log_msg = f"SUBAGENT_START: session {session_id}" if session_id else "SUBAGENT_START"
        if agent_id:
            log_msg += f", agent_id={agent_id}"
        if subagent_id:
            log_msg += f", subagent_id={subagent_id}"
        self.logger.debug(log_msg)

        # Track pending subagent depth for auto-registration
        if session_id and subagent_id and self._session_manager:
            try:
                row = self._session_manager.db.fetchone(
                    "SELECT agent_depth FROM sessions WHERE external_id = ? AND status = 'active'"
                    " ORDER BY updated_at DESC LIMIT 1",
                    (session_id,),
                )
                parent_depth = (row["agent_depth"] or 0) if row else 0
                self._pending_subagent_depths[subagent_id] = parent_depth + 1
                self.logger.debug(
                    f"Pending subagent depth for {subagent_id}: {parent_depth + 1}",
                )
            except Exception as e:
                self.logger.debug(f"Failed to track subagent depth: {e}")

        # Toggle is_subagent so rule engine unblocks native task tools
        if session_id and self._session_manager:
            try:
                from gobby.workflows.state_manager import SessionVariableManager

                sv_mgr = SessionVariableManager(self._session_manager.db)
                sv_mgr.set_variable(session_id, "is_subagent", True)
                self.logger.debug(f"Set is_subagent=True for session {session_id}")
            except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
                self.logger.warning(f"Failed to set is_subagent on SUBAGENT_START: {e}")

        return HookResponse(decision="allow")

    def handle_subagent_stop(self, event: HookEvent) -> HookResponse:
        """Handle SUBAGENT_STOP event."""
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"SUBAGENT_STOP: session {session_id}")
        else:
            self.logger.debug("SUBAGENT_STOP")

        # Clear is_subagent so rule engine re-blocks native task tools
        if session_id and self._session_manager:
            try:
                from gobby.workflows.state_manager import SessionVariableManager

                sv_mgr = SessionVariableManager(self._session_manager.db)
                sv_mgr.set_variable(session_id, "is_subagent", False)
                self.logger.debug(f"Set is_subagent=False for session {session_id}")
            except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
                self.logger.warning(f"Failed to clear is_subagent on SUBAGENT_STOP: {e}")

        return HookResponse(decision="allow")
