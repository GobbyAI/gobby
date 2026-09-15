from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Literal

import psycopg

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse, SessionSource
from gobby.hooks.session_types import has_prior_session_activity
from gobby.sessions.reasoning_effort import observed_reasoning_effort
from gobby.sessions.title_lifecycle import promote_heuristic_title
from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.capability_routing import (
    capability_menu,
    gobby_help_prefix,
    route_gobby_request,
    standalone_menu,
)
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hook_receipts import retire_session_hook_effects

logger = logging.getLogger(__name__)

# Pattern for slash-router /gobby or Codex $gobby commands with optional args.
_GOBBY_CMD_PATTERN = re.compile(
    r"^[/\$]gobby(?::(\S+))?(?:\s+(.*)|\s*)$",
    re.IGNORECASE | re.DOTALL,
)


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

    def _ensure_bound_grok_native_subagent(self, event: HookEvent, session_id: str) -> None:
        """Derive is_subagent when a Grok child hook inherited the parent TTY."""
        if event.source != SessionSource.GROK:
            return
        if not event.metadata.get("_native_subagent_binding"):
            return
        if self._session_manager is None:
            return
        from gobby.storage.sessions._contested_expiry import session_has_active_native_subagent

        if session_has_active_native_subagent(self._session_manager.db, session_id):
            return
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            SessionVariableManager(self._session_manager.db).adjust_counter_and_derive_boolean(
                session_id,
                "subagent_count",
                1,
                boolean_name="is_subagent",
            )
        except (psycopg.Error, KeyError, TypeError, ValueError) as exc:
            self.logger.warning(
                "Failed to derive Grok is_subagent from inherited TTY: %s",
                exc,
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

            if self._session_manager and not event.metadata.get("_native_subagent_binding"):
                try:
                    promote_heuristic_title(self._session_manager, session_id, prompt)
                    effort = observed_reasoning_effort(input_data)
                    if effort is not None:
                        self._session_manager.update(session_id, reasoning_effort=effort)
                except (psycopg.Error, KeyError, TypeError, ValueError) as e:
                    self.logger.warning("Failed to persist session prompt metadata: %s", e)

            # A new parent turn cannot inherit live subagents from the previous
            # turn. Reset both values together to recover from missed stop hooks.
            # Child Grok conversations inherit the parent TTY and bind here;
            # that is not a parent turn and must not clear is_subagent.
            if self._session_manager and not event.metadata.get("_native_subagent_binding"):
                try:
                    from gobby.workflows.state_manager import SessionVariableManager

                    sv_mgr = SessionVariableManager(self._session_manager.db)
                    sv_mgr.merge_variables(
                        session_id,
                        {"subagent_count": 0, "is_subagent": False},
                    )
                except (psycopg.Error, KeyError, TypeError, ValueError) as e:
                    self.logger.warning("Failed to reset subagent count on BEFORE_AGENT: %s", e)
            elif self._session_manager:
                self._ensure_bound_grok_native_subagent(event, session_id)

            try:
                from gobby.hooks.event_handlers._session_start.transcripts import (
                    ensure_qwen_transcript_tracking,
                )

                ensure_qwen_transcript_tracking(self, event, session_id)
            except Exception as e:
                self.logger.warning("Failed to register deferred Qwen transcript: %s", e)

            # Start a fresh lifecycle generation (unless /clear or /exit).
            prompt_lower = stripped_prompt.lower()
            if prompt_lower not in ("/clear", "/exit"):
                if not self._skip_session_status_update_during_shutdown(
                    "BEFORE_AGENT", session_id, "active"
                ):
                    self._begin_turn_lifecycle(event)

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

        # Help is a terminal display operation, including the first parent turn.
        # Leave instruction-injection markers untouched for the next work turn.
        if gobby_help_prefix(stripped_prompt):
            command_prefix = "$gobby" if event.source.value == "codex" else "/gobby"
            content = self._generate_help_content(session_id, command_prefix, project_id)
            if event.source.value == "grok":
                # Grok discards allowing prompt-hook context, but displays a
                # blocked prompt's reason before invoking the model.
                self._end_turn_lifecycle(event, "completed")
                return HookResponse(
                    decision="block", reason=content.partition("\n\n")[2] or content
                )
            return HookResponse(
                decision="allow",
                context=content,
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
        definition_surface: Literal["spawn", "persona"] = (
            "spawn" if prompt_surface == "agent" else "persona"
        )
        if not agent_body.supports_surface(definition_surface):
            return
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
        Supports space syntax (/gobby plan, $gobby plan) and legacy slash-router
        colon syntax.
        """
        match = _GOBBY_CMD_PATTERN.match(prompt)
        if not match:
            return None

        command_prefix = "$gobby" if prompt.startswith("$") else "/gobby"
        project_kwargs = {"project_id": project_id} if project_id is not None else {}
        skill_name = match.group(1)  # None for bare /gobby or space syntax
        args = (match.group(2) or "").strip()

        request = " ".join(part for part in (skill_name, args) if part)
        if not request or request.lower() in ("help", "skill"):
            return self._generate_help_content(
                session_id, command_prefix=command_prefix, **project_kwargs
            )
        manager = self._skill_manager
        if manager is None:
            raise RuntimeError("skill_manager not initialized")
        route = route_gobby_request(
            request,
            resolve_skill=lambda name: manager.resolve_skill_name(name, **project_kwargs),
            command_prefix=command_prefix,
        )
        if route.kind == "help":
            return self._generate_help_content(
                session_id, command_prefix=command_prefix, **project_kwargs
            )
        if route.kind == "unknown":
            return self._skill_not_found_context(
                route.unknown_name or request,
                **({"session_id": session_id} if session_id else {}),
                command_prefix=command_prefix,
                **project_kwargs,
            )
        return route.context

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
        """Render a complete menu within both the character and UTF-8 byte ceiling."""
        from gobby.config.skills import SkillsConfig
        from gobby.skills.authoring import resolve_bundled_max_content_size

        directive = "Display this help immediately and finish. Make zero tool calls.\n\n"
        try:
            if self._skill_manager is None:
                raise RuntimeError("skill_manager not initialized")
            limit = SkillsConfig().bundled_max_content_size
            if self._session_manager:
                limit = resolve_bundled_max_content_size(self._session_manager.db)
            skills = self._router_skills(session_id, project_id)
            catalog = load_capability_catalog()
            for description_limit in (120, 60, 0):
                skills_list = (
                    standalone_menu(skills, catalog, command_prefix, description_limit)
                    or "No installed standalone skills."
                )
                capabilities_list = capability_menu(catalog, command_prefix, description_limit)
                fallback = (
                    directive
                    + "# Gobby\n\nCapabilities:\n\n"
                    + capabilities_list
                    + "\n\nInstalled skills:\n\n"
                    + skills_list
                    + f"\n\nUse `{command_prefix} <capability> references` for topics."
                )
                content = _load_agent_prompt(
                    "help-content",
                    {
                        "skills_list": skills_list,
                        "capabilities_list": capabilities_list,
                        "command_prefix": command_prefix,
                    },
                    fallback,
                )
                if max(len(content), len(content.encode("utf-8"))) <= limit:
                    return content
            return (
                directive
                + "Gobby help exceeds the display limit. "
                + f"Use `{command_prefix} skills` to browse skills."
            )
        except Exception:
            self.logger.exception("Gobby help unavailable for project %s", project_id)
            return directive + "Gobby help is unavailable."

    def _skill_not_found_context(
        self,
        name: str,
        command_prefix: str = "/gobby",
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Generate context for an unrecognized skill name."""
        if self._skill_manager is None:
            raise RuntimeError("skill_manager not initialized")
        skills = self._router_skills(session_id, project_id)

        # Find close matches (name contains or starts with input)
        name_lower = name.lower()
        close = sorted(
            s.name
            for s in skills
            if not s.is_always_apply()
            and not s.is_internal()
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

        context = _load_agent_prompt(
            "skill-not-found",
            {
                "skill_name": name,
                "close_matches": close,
                "command_prefix": command_prefix,
            },
            fallback,
        )
        return (
            context
            + "\n\n"
            + self._generate_help_content(
                session_id, command_prefix=command_prefix, project_id=project_id
            )
        )

    def handle_after_agent(self, event: HookEvent) -> HookResponse:
        """Handle AFTER_AGENT event."""
        self._apply_attention_metadata_report(event)
        session_id = event.metadata.get("_platform_session_id")
        cli_source = event.source.value

        context_parts: list[str] = []

        if session_id:
            self.logger.debug("AFTER_AGENT: session %s, cli=%s", session_id, cli_source)
            if (
                event.turn_disposition != "unknown"
                and not self._skip_session_status_update_during_shutdown(
                    "AFTER_AGENT", session_id, "paused"
                )
            ):
                self._end_turn_lifecycle(event, event.turn_disposition)
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
                if (
                    event.turn_disposition != "unknown"
                    and not self._skip_session_status_update_during_shutdown(
                        "STOP", session_id, "paused"
                    )
                ):
                    self._end_turn_lifecycle(event, event.turn_disposition)
                db = getattr(self._session_manager, "db", None)
                if db is not None:
                    try:
                        retire_session_hook_effects(db, session_id=session_id)
                    except Exception as e:
                        self.logger.warning(
                            "Failed to retire hook effects on STOP for session %s: %s",
                            session_id,
                            e,
                        )
        else:
            self.logger.debug("STOP")

        response = HookResponse(
            decision="allow",
            context="\n\n".join(context_parts) if context_parts else None,
        )
        self._apply_debug_echo(response)
        return response

    def handle_interrupt(self, event: HookEvent) -> HookResponse:
        """Handle explicit whole-turn user interruption evidence."""
        self._end_turn_lifecycle(event, "user_interrupted")
        return HookResponse(decision="allow")

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
                    "PRE_COMPACT", session_id, "awaiting_handoff"
                ):
                    self._session_manager.update_session_status(session_id, "awaiting_handoff")
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
