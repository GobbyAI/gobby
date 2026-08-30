from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Literal

import psycopg

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.session_types import has_prior_session_activity
from gobby.skills.formatting import skill_fetch_directive

logger = logging.getLogger(__name__)

# Pattern for slash-router /gobby or Codex $gobby commands with optional args.
_GOBBY_CMD_PATTERN = re.compile(
    r"^[/\$]gobby(?::(\S+))?(?:\s+(.*)|\s*)$",
    re.IGNORECASE | re.DOTALL,
)
_HELP_SKILL_LIST_LIMIT = 50


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

    def _set_attention_metadata(
        self,
        event: HookEvent,
        *,
        text: object,
        ttl_ms: object,
    ) -> None:
        store = getattr(self, "_attention_metadata_store", None)
        session_id = event.metadata.get("_platform_session_id")
        if store is None or not isinstance(session_id, str) or not session_id:
            return
        try:
            store.set(f"session:{session_id}", text, ttl_ms)
        except (TypeError, ValueError) as exc:
            self.logger.warning("Dropped invalid attention metadata self-report: %s", exc)

    def _apply_attention_metadata_report(self, event: HookEvent) -> None:
        report = event.data.get("attention_metadata")
        if report is None:
            return
        if not isinstance(report, Mapping) or set(report) != {"text", "ttl_ms"}:
            self.logger.warning("Dropped invalid attention metadata self-report payload")
            return
        self._set_attention_metadata(
            event,
            text=report["text"],
            ttl_ms=report["ttl_ms"],
        )

    def handle_before_agent(self, event: HookEvent) -> HookResponse:
        """Handle BEFORE_AGENT event (user prompt submit)."""
        self._apply_attention_metadata_report(event)
        input_data = event.data
        prompt = input_data.get("prompt", "")
        stripped_prompt = prompt.strip()
        session_id = event.metadata.get("_platform_session_id")
        project_id = event.project_id or self._resolve_project_id(event.project_id, event.cwd)

        context_parts = []

        if session_id:
            self.logger.debug("BEFORE_AGENT: session %s, prompt_len=%s", session_id, len(prompt))

            # A new parent turn cannot inherit live subagents from the previous
            # turn. Reset both values together to recover from missed stop hooks.
            if self._session_manager:
                try:
                    from gobby.workflows.state_manager import SessionVariableManager

                    sv_mgr = SessionVariableManager(self._session_manager.db)
                    sv_mgr.merge_variables(
                        session_id,
                        {"subagent_count": 0, "is_subagent": False},
                    )
                except (psycopg.Error, KeyError, TypeError, ValueError) as e:
                    self.logger.warning("Failed to reset subagent count on BEFORE_AGENT: %s", e)

            try:
                from gobby.hooks.event_handlers._session_start.transcripts import (
                    ensure_qwen_transcript_tracking,
                )

                ensure_qwen_transcript_tracking(self, event, session_id)
            except Exception as e:
                self.logger.warning("Failed to register deferred Qwen transcript: %s", e)

            # Update status to active (unless /clear or /exit)
            prompt_lower = stripped_prompt.lower()
            if prompt_lower not in ("/clear", "/exit") and self._session_manager:
                if not self._skip_session_status_update_during_shutdown(
                    "BEFORE_AGENT", session_id, "active"
                ):
                    try:
                        self._session_manager.update_session_status(
                            session_id,
                            "active",
                            activity_confirmed=True,
                        )
                    except Exception as e:
                        self.logger.warning("Failed to update session status: %s", e)

            # Generate boundary summaries before clear/exit.
            if prompt_lower in ("/clear", "/exit"):
                self.logger.debug("Detected %s - generating session summaries", prompt_lower)
                try:
                    if self._dispatch_session_summaries_fn:
                        self._dispatch_session_summaries_fn(session_id, False, None, False)
                except Exception as e:
                    self.logger.warning(
                        "Failed to generate session summaries on %s: %s", prompt_lower, e
                    )

        # Skill interception — runs before lifecycle workflows
        if self._skill_manager and stripped_prompt:
            # ``stripped_prompt`` is truthy here, so split() always has a first token.
            skill_identifier = stripped_prompt.split(None, 1)[0]
            try:
                skill_context = self._intercept_skill_command(
                    stripped_prompt,
                    session_id,
                    project_id,
                )
                if skill_context:
                    context_parts.append(skill_context)
                else:
                    # Try trigger-based suggestion for non-command prompts
                    suggestion = self._suggest_skills(stripped_prompt, project_id)
                    if suggestion:
                        context_parts.append(suggestion)
            except Exception as e:
                self.logger.exception(
                    "Failed skill interception for %s: %s",
                    skill_identifier,
                    e,
                )

        response = HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )

        # Inject prompt-facing agent context on first before_agent. SessionStart
        # only activates session variables and non-prompt metadata.
        if session_id:
            try:
                self._inject_agent_instructions_if_needed(event, session_id, response)
            except Exception as e:
                self.logger.exception("Failed to inject agent instructions: %s", e)

        self._apply_debug_echo(response)
        return response

    def _inject_agent_instructions_if_needed(
        self, event: HookEvent, session_id: str, response: HookResponse
    ) -> None:
        """Format agent preamble on first before_agent.

        Everything needed is already in DB from SessionStart activation:
        - Lifecycle/enforcement identity: _agent_type session variable
        - Interactive prompt identity: _persona_name session variable
        - Agent definition: agent_definitions table
        """
        if not self._session_manager:
            return

        from gobby.workflows.state_manager import SessionVariableManager

        sv_mgr = SessionVariableManager(self._session_manager.db)
        variables = sv_mgr.get_variables(session_id)

        identity_reinject = bool(variables.get("_agent_identity_reinject"))
        rehydrate_pending = bool(variables.get("_agent_context_rehydrate_pending"))

        if (
            variables.get("_agent_context_injected")
            and not identity_reinject
            and not rehydrate_pending
        ):
            return

        # Get project_id for project-specific agent resolution
        project_id = None
        session_row = None
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

        if (
            not identity_reinject
            and not rehydrate_pending
            and has_prior_session_activity(session_row)
        ):
            sv_mgr.merge_variables(session_id, {"_agent_context_injected": True})
            return

        is_spawned_agent = bool(variables.get("is_spawned_agent"))
        agent_name = variables.get("_agent_type", "default")
        if not is_spawned_agent:
            persona_name = variables.get("_persona_name")
            if isinstance(persona_name, str) and persona_name:
                agent_name = persona_name

        from gobby.workflows.agent_resolver import resolve_agent

        agent_body = resolve_agent(agent_name, self._session_manager.db, project_id=project_id)
        if not agent_body:
            return

        prompt_surface: Literal["persona", "agent"] = "agent" if is_spawned_agent else "persona"
        preamble = agent_body.prompt_for(prompt_surface)
        if preamble:
            if response.context:
                response.context = f"{preamble}\n\n{response.context}"
            else:
                response.context = preamble

        from gobby.hooks.receipt_effects import (
            STAGED_EFFECTS_FIELD,
            merge_staged_payloads,
            record_worker_staging,
        )

        staged = {
            "session_id": session_id,
            "session_variables": {
                "_agent_context_injected": True,
                "_agent_identity_reinject": False,
                "_agent_context_rehydrate_pending": False,
            },
        }
        existing = response.metadata.get(STAGED_EFFECTS_FIELD)
        response.metadata[STAGED_EFFECTS_FIELD] = merge_staged_payloads(
            existing if isinstance(existing, dict) else {},
            staged,
        )
        record_worker_staging(staged)

    def _intercept_skill_command(
        self,
        prompt: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> str | None:
        """Intercept /gobby or $gobby skill commands.

        Returns context string to add, or None if not a Gobby router command.
        Supports space syntax (/gobby expand, $gobby expand) and legacy slash-router
        colon syntax.
        """
        match = _GOBBY_CMD_PATTERN.match(prompt)
        if not match:
            return None

        command_prefix = "$gobby" if prompt.startswith("$") else "/gobby"
        project_kwargs = {"project_id": project_id} if project_id is not None else {}
        skill_name = match.group(1)  # None for bare /gobby or space syntax
        args = (match.group(2) or "").strip()

        # Support space syntax: Gobby expand → treat first word of args as skill name.
        # Also supports Gobby skill(s) <name> as a namespace prefix.
        resolved = None
        if not skill_name and args and self._skill_manager:
            parts = args.split(None, 1)
            first_word = parts[0]
            if first_word.lower() in ("skill", "skills"):
                # Gobby skill(s) <name> → shift to second word.
                if len(parts) > 1:
                    sub_parts = parts[1].split(None, 1)
                    skill_name = sub_parts[0]
                    args = sub_parts[1] if len(sub_parts) > 1 else ""
                    resolved = self._skill_manager.resolve_skill_name(skill_name, **project_kwargs)
                # Bare Gobby skills → fall through to help.
            elif first_word.lower() != "help":
                skill_name = first_word
                resolved = self._skill_manager.resolve_skill_name(first_word, **project_kwargs)
                if resolved:
                    args = parts[1] if len(parts) > 1 else ""

        # Bare Gobby or Gobby help → generate help.
        if not skill_name or skill_name.lower() == "help":
            return self._generate_help_content(
                session_id,
                command_prefix=command_prefix,
                **project_kwargs,
            )

        # Gobby skillname → resolve and direct the agent to fetch it on demand.
        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        skill = (
            resolved
            if resolved
            else self._skill_manager.resolve_skill_name(skill_name, **project_kwargs)
        )

        if not skill:
            return self._skill_not_found_context(
                skill_name,
                command_prefix=command_prefix,
                **project_kwargs,
            )

        return skill_fetch_directive(skill.name)

    def _suggest_skills(self, prompt: str, project_id: str | None = None) -> str | None:
        """Suggest skills based on trigger keyword matching.

        Only runs for non-command prompts. Returns a lightweight hint
        if a strong match is found (score >= 0.7).
        """
        # Skip if it looks like a native command.
        if prompt.startswith(("/", "$")):
            return None

        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        matches = self._skill_manager.match_triggers(
            prompt,
            threshold=0.7,
            project_id=project_id,
        )

        if not matches:
            return None

        skill, score = matches[0]
        fallback = f"Relevant skill available. {skill_fetch_directive(skill.name)}"
        return _load_agent_prompt("skill-hint", {"skill_name": skill.name}, fallback)

    def _generate_help_content(
        self,
        session_id: str | None = None,
        command_prefix: str = "/gobby",
        project_id: str | None = None,
    ) -> str:
        """Generate help content listing all available skills."""
        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        skills = self._skill_manager.discover_core_skills(project_id)

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
            except Exception as e:
                self.logger.warning(
                    "Failed to filter help content by active skills for session %s: %s",
                    session_id,
                    e,
                )

        # Sort alphabetically, skip always-apply skills and the router entrypoint.
        user_skills = sorted(
            [s for s in skills if not s.is_always_apply() and s.name != "gobby"],
            key=lambda s: s.name,
        )

        skill_lines = []
        for skill in user_skills[:_HELP_SKILL_LIST_LIMIT]:
            desc = skill.description.split(".")[0] if skill.description else ""
            skill_lines.append(f"- `{command_prefix} {skill.name}` — {desc}")
        hidden_count = len(user_skills) - len(skill_lines)
        if hidden_count > 0:
            skill_lines.append(
                f"- ... {hidden_count} more skills. Use `list_skills()` on `gobby-skills`."
            )
        skills_list = "\n".join(skill_lines)

        fallback = (
            "# Gobby Skills\n\n"
            "Installed skills below are generated from `discover_core_skills()`. "
            f"Invoke one with `{command_prefix} <skill>`:\n\n"
            f"{skills_list}\n\n"
            '**Skill discovery**: `list_skills()` / `get_skill(name="skill-name")` '
            "on `gobby-skills`.\n"
            '**Hub search**: `search_hub(query="...")` on `gobby-skills`.\n'
            "**MCP tools**: call leased known tools directly. For a known unleased tool, "
            "call `get_tool_schema` directly, then `call_tool`. Use `list_tools` only for "
            "an unknown tool name and `list_mcp_servers` only for unknown server or "
            "registry inspection."
        )

        return _load_agent_prompt(
            "help-content",
            {"skills_list": skills_list, "command_prefix": command_prefix},
            fallback,
        )

    def _skill_not_found_context(
        self,
        name: str,
        command_prefix: str = "/gobby",
        project_id: str | None = None,
    ) -> str:
        """Generate context for an unrecognized skill name."""
        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        skills = self._skill_manager.discover_core_skills(project_id)

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
                lines.append(f"  - `{command_prefix} {match}`")
        lines.extend(
            [
                "",
                f"Run `{command_prefix}` or `{command_prefix} help` to see all available skills.",
            ]
        )
        fallback = "\n".join(lines)

        return _load_agent_prompt(
            "skill-not-found",
            {
                "skill_name": name,
                "close_matches": close,
                "command_prefix": command_prefix,
            },
            fallback,
        )

    def handle_after_agent(self, event: HookEvent) -> HookResponse:
        """Handle AFTER_AGENT event."""
        self._apply_attention_metadata_report(event)
        session_id = event.metadata.get("_platform_session_id")
        cli_source = event.source.value

        context_parts: list[str] = []

        if session_id:
            self.logger.debug("AFTER_AGENT: session %s, cli=%s", session_id, cli_source)
            if self._session_manager:
                if not self._skip_session_status_update_during_shutdown(
                    "AFTER_AGENT", session_id, "paused"
                ):
                    try:
                        self._session_manager.update_session_status(
                            session_id,
                            "paused",
                            activity_confirmed=True,
                        )
                    except Exception as e:
                        self.logger.warning("Failed to update session status: %s", e)
        else:
            self.logger.debug("AFTER_AGENT: cli=%s", cli_source)

        response = HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )
        self._apply_debug_echo(response)
        return response

    def handle_stop(self, event: HookEvent) -> HookResponse:
        """Handle an agent STOP event."""
        session_id = event.metadata.get("_platform_session_id")

        context_parts: list[str] = []

        if session_id:
            self.logger.debug("STOP: session %s", session_id)
            if self._session_manager:
                if not self._skip_session_status_update_during_shutdown(
                    "STOP", session_id, "paused"
                ):
                    try:
                        self._session_manager.update_session_status(
                            session_id,
                            "paused",
                            activity_confirmed=True,
                        )
                    except Exception as e:
                        self.logger.warning("Failed to update session status: %s", e)
        else:
            self.logger.debug("STOP")

        response = HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )
        self._apply_debug_echo(response)
        return response

    def handle_pre_compact(self, event: HookEvent) -> HookResponse:
        """Handle PRE_COMPACT event."""
        self._set_attention_metadata(event, text="compacting", ttl_ms=60_000)
        trigger = event.data.get("trigger", "auto")
        session_id = event.metadata.get("_platform_session_id")

        is_handoff_trigger = trigger in {"manual", "user", "compact"}

        if session_id:
            self.logger.debug("PRE_COMPACT (%s): session %s", trigger, session_id)
            # Auto compaction in Codex is an in-session event, not a handoff.
            if is_handoff_trigger and self._session_manager:
                if not self._skip_session_status_update_during_shutdown(
                    "PRE_COMPACT", session_id, "handoff_ready"
                ):
                    self._session_manager.update_session_status(session_id, "handoff_ready")
            # Generate session summaries from digest before compaction
            try:
                if self._dispatch_session_summaries_fn:
                    self._dispatch_session_summaries_fn(
                        session_id,
                        False,
                        None,
                        False,
                    )
            except Exception as e:
                self.logger.warning("Failed to generate session summaries on compact: %s", e)
        else:
            self.logger.debug("PRE_COMPACT (%s)", trigger)

        return HookResponse(decision="allow")

    def handle_subagent_start(self, event: HookEvent) -> HookResponse:
        """Handle SUBAGENT_START event.

        Increments subagent_count and derives is_subagent so the rule engine
        unblocks native task tools while any subagent remains active.
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

        # Count active subagents so one stop cannot hide another live subagent.
        if session_id and self._session_manager:
            try:
                from gobby.workflows.state_manager import SessionVariableManager

                sv_mgr = SessionVariableManager(self._session_manager.db)
                count = sv_mgr.adjust_counter_and_derive_boolean(
                    session_id,
                    "subagent_count",
                    1,
                    boolean_name="is_subagent",
                )
                self.logger.debug("Set subagent_count=%s for session %s", count, session_id)
            except (psycopg.Error, KeyError, TypeError, ValueError) as e:
                self.logger.warning("Failed to increment subagent_count on SUBAGENT_START: %s", e)

        return HookResponse(decision="allow")

    def handle_subagent_stop(self, event: HookEvent) -> HookResponse:
        """Handle SUBAGENT_STOP event."""
        session_id = event.metadata.get("_platform_session_id")

        if session_id:
            self.logger.debug("SUBAGENT_STOP: session %s", session_id)
        else:
            self.logger.debug("SUBAGENT_STOP")

        # Clamp at zero and derive is_subagent from the remaining count.
        if session_id and self._session_manager:
            try:
                from gobby.workflows.state_manager import SessionVariableManager

                sv_mgr = SessionVariableManager(self._session_manager.db)
                count = sv_mgr.adjust_counter_and_derive_boolean(
                    session_id,
                    "subagent_count",
                    -1,
                    boolean_name="is_subagent",
                )
                self.logger.debug("Set subagent_count=%s for session %s", count, session_id)
            except (psycopg.Error, KeyError, TypeError, ValueError) as e:
                self.logger.warning("Failed to decrement subagent_count on SUBAGENT_STOP: %s", e)

        return HookResponse(decision="allow")
