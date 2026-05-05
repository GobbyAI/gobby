"""Session start event handler compatibility boundary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.hooks.event_handlers._base import EventHandlersBase
from gobby.hooks.events import HookEvent, HookResponse
from gobby.sessions.compact_continuation import consume_and_schedule_compact_self_continuation
from gobby.workflows.summary_actions import schedule_tmux_window_rename

from .types import AgentActivationResult

if TYPE_CHECKING:
    from gobby.storage.session_models import Session

SUMMARY_GENERATION_TIMEOUT_S = 120

__all__ = [
    "AgentActivationResult",
    "Path",
    "SUMMARY_GENERATION_TIMEOUT_S",
    "SessionStartMixin",
    "consume_and_schedule_compact_self_continuation",
    "schedule_tmux_window_rename",
]


class SessionStartMixin(EventHandlersBase):
    """Mixin for handling SESSION_START events and related helpers."""

    def _derive_transcript_path(
        self,
        cli_source: str,
        input_data: dict[str, Any],
        external_id: str,
    ) -> str | None:
        """Derive transcript path for CLIs that do not provide one natively."""
        from .transcripts import derive_transcript_path

        return derive_transcript_path(self, cli_source, input_data, external_id)

    def _find_gemini_transcript(
        self,
        input_data: dict[str, Any],
        external_id: str,
    ) -> str | None:
        """Locate a Gemini CLI JSON session transcript for the hook event."""
        from .transcripts import find_gemini_transcript

        return find_gemini_transcript(self, input_data, external_id)

    def _find_qwen_transcript(
        self,
        input_data: dict[str, Any],
        external_id: str,
    ) -> str | None:
        """Locate a Qwen CLI JSON session transcript for the hook event."""
        from .transcripts import find_qwen_transcript

        return find_qwen_transcript(self, input_data, external_id)

    def _find_json_session_transcript(
        self,
        cli_name: str,
        cli_label: str,
        input_data: dict[str, Any],
        external_id: str,
    ) -> str | None:
        """Find a JSON session transcript for supported CLIs."""
        from .transcripts import find_json_session_transcript

        return find_json_session_transcript(self, cli_name, cli_label, input_data, external_id)

    def handle_session_start(self, event: HookEvent) -> HookResponse:
        """Handle SESSION_START event."""
        from .flow import handle_session_start

        return handle_session_start(self, event)

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
        """Handle session start for a pre-created session."""
        from .flow import handle_pre_created_session

        return handle_pre_created_session(
            self,
            existing_session,
            external_id,
            transcript_path,
            cli_source,
            event,
            cwd,
            terminal_context,
        )

    def _resolve_agent_name(
        self,
        session_id: str,
        agent_name_override: str | None,
    ) -> str:
        """Determine which agent to activate."""
        from .agents import resolve_agent_name

        return resolve_agent_name(self, session_id, agent_name_override)

    def _build_agent_changes(
        self,
        agent_body: Any,
        session_id: str,
        enabled_rules: list[Any],
        all_skills: list[Any],
        enabled_variables: list[Any],
    ) -> tuple[dict[str, Any], set[str], set[str] | None]:
        """Build session variable changes from agent definition, rules, skills, and variables."""
        from .agents import build_agent_changes

        return build_agent_changes(
            self,
            agent_body,
            session_id,
            enabled_rules,
            all_skills,
            enabled_variables,
        )

    def _setup_code_index(self, session_id: str | None, project_id: str | None) -> None:
        """Set code_index_available session variable if the project has an index."""
        from .agents import setup_code_index

        setup_code_index(self, session_id, project_id)

    def _activate_default_agent(
        self,
        session_id: str,
        cli_source: str,
        project_id: str | None,
        agent_name_override: str | None = None,
    ) -> AgentActivationResult | None:
        """Activate the default agent for a session, merging its properties."""
        from .agents import activate_default_agent

        return activate_default_agent(
            self,
            session_id,
            cli_source,
            project_id,
            agent_name_override=agent_name_override,
        )

    def _get_claimed_task_info(
        self,
        session_id: str | None,
        project_id: str | None,
    ) -> list[tuple[str, str, str]] | None:
        """Delegate to module-level get_claimed_task_info."""
        from gobby.hooks.event_handlers._session_responses import get_claimed_task_info

        return get_claimed_task_info(self, session_id, project_id)

    def _build_claimed_task_context(
        self,
        session_id: str,
        project_id: str | None,
    ) -> str | None:
        """Delegate to module-level build_claimed_task_context."""
        from gobby.hooks.event_handlers._session_responses import build_claimed_task_context

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
        from gobby.hooks.event_handlers._session_responses import compose_session_response

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
