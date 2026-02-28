from __future__ import annotations

import logging
import re
from typing import Any

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse, SessionSource

logger = logging.getLogger(__name__)

# Pattern for /gobby or /gobby:skillname with optional args
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
        logger.debug("Failed to load agent prompt %s, using fallback", name, exc_info=True)
        return fallback


class AgentEventHandlerMixin(EventHandlersBase):
    """Mixin for handling agent-related events."""

    def handle_before_agent(self, event: HookEvent) -> HookResponse:
        """Handle BEFORE_AGENT event (user prompt submit)."""
        input_data = event.data
        prompt = input_data.get("prompt", "")
        transcript_path = input_data.get("transcript_path")
        session_id = event.metadata.get("_platform_session_id")

        context_parts = []

        if session_id:
            self.logger.debug(f"BEFORE_AGENT: session {session_id}, prompt_len={len(prompt)}")

            # Update status to active (unless /clear or /exit)
            prompt_lower = prompt.strip().lower()
            if prompt_lower not in ("/clear", "/exit") and self._session_manager:
                try:
                    self._session_manager.update_session_status(session_id, "active")
                    if self._session_storage:
                        self._session_storage.reset_transcript_processed(session_id)
                except Exception as e:
                    self.logger.warning(f"Failed to update session status: {e}")

            # Handle /clear command - lifecycle workflows handle handoff
            if prompt_lower in ("/clear", "/exit") and transcript_path:
                self.logger.debug(f"Detected {prompt_lower} - lifecycle workflows handle handoff")

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
        self._apply_debug_echo(response)
        return response

    def _intercept_skill_command(self, prompt: str, session_id: str | None = None) -> str | None:
        """Intercept /gobby and /gobby:skillname commands.

        Returns context string to inject, or None if not a /gobby command.
        Supports both colon syntax (/gobby:expand) and space syntax (/gobby expand).
        """
        match = _GOBBY_CMD_PATTERN.match(prompt)
        if not match:
            return None

        skill_name = match.group(1)  # None for bare /gobby or space syntax
        args = (match.group(2) or "").strip()

        # Support space syntax: /gobby expand → treat first word of args as skill name
        resolved = None
        if not skill_name and args and self._skill_manager:
            parts = args.split(None, 1)
            first_word = parts[0]
            if first_word.lower() != "help":
                skill_name = first_word
                resolved = self._skill_manager.resolve_skill_name(first_word)
                if resolved:
                    args = parts[1] if len(parts) > 1 else ""

        # /gobby or /gobby help → generate help
        if not skill_name or skill_name.lower() == "help":
            return self._generate_help_content(session_id)

        # /gobby:skillname → resolve and inject
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

        if session_id and self._session_storage:
            try:
                from gobby.workflows.state_manager import SessionVariableManager

                sv_mgr = SessionVariableManager(self._session_storage.db)
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
            skill_lines.append(f"- `/gobby:{skill.name}` — {desc}")
        skills_list = "\n".join(skill_lines)

        fallback = (
            "# Gobby Skills\n\n"
            "Invoke skills directly with `/gobby:skillname` syntax:\n\n"
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
                lines.append(f"  - `/gobby:{match}`")
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

        Note: Gemini fires PreCompress constantly during normal operation,
        unlike Claude which fires it only when approaching context limits.
        We skip handoff logic and workflow execution for Gemini to avoid
        excessive state changes and workflow interruptions.
        """
        trigger = event.data.get("trigger", "auto")
        session_id = event.metadata.get("_platform_session_id")

        # Skip handoff logic for Gemini - it fires PreCompress too frequently
        if event.source == SessionSource.GEMINI:
            self.logger.debug(f"PRE_COMPACT ({trigger}): session {session_id} [Gemini - skipped]")
            return HookResponse(decision="allow")

        if session_id:
            self.logger.debug(f"PRE_COMPACT ({trigger}): session {session_id}")
            # Mark session as handoff_ready so it can be found as parent after compact
            if self._session_manager:
                self._session_manager.update_session_status(session_id, "handoff_ready")
        else:
            self.logger.debug(f"PRE_COMPACT ({trigger})")

        return HookResponse(decision="allow")

    def handle_subagent_start(self, event: HookEvent) -> HookResponse:
        """Handle SUBAGENT_START event."""
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

        return HookResponse(decision="allow")

    def handle_subagent_stop(self, event: HookEvent) -> HookResponse:
        """Handle SUBAGENT_STOP event."""
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug(f"SUBAGENT_STOP: session {session_id}")
        else:
            self.logger.debug("SUBAGENT_STOP")

        return HookResponse(decision="allow")
