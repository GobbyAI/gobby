"""Tests for hooks/event_handlers/_agent.py — targeting uncovered lines."""

from __future__ import annotations

from datetime import datetime
from importlib.resources import files
from typing import cast
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from gobby.hooks.event_handlers._agent import (
    _GOBBY_CMD_PATTERN,
    AgentEventHandlerMixin,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_AGENT,
    session_id: str = "ext-123",
    source: SessionSource = SessionSource.CLAUDE,
    data: dict | None = None,
    metadata: dict | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=source,
        timestamp=datetime.now(),
        data=data or {},
        metadata=metadata or {},
    )


def _resolve_none() -> None:
    return None


class _TestHandler(AgentEventHandlerMixin):
    """Concrete implementation with required attributes for testing."""

    def __init__(self) -> None:
        self.logger = MagicMock()
        self._session_manager = MagicMock()
        self._session_coordinator = None
        self._message_processor_resolver = _resolve_none
        self._task_manager = None
        self._workflow_handler = None
        self._workflow_config_resolver = _resolve_none
        self._skill_manager = MagicMock()
        self._session_task_manager = None
        self._dispatch_session_summaries_fn = MagicMock()
        self._get_machine_id = MagicMock(return_value="21000000-0000-4000-8000-000000000001")
        self._resolve_project_id = MagicMock(return_value="proj-1")
        self._handler_map = {}


# ---------------------------------------------------------------------------
# _load_agent_prompt tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# handle_before_agent tests
# ---------------------------------------------------------------------------


class TestHandleBeforeAgent:
    """Tests for handle_before_agent."""

    def test_no_session_id(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(data={"prompt": "hello"}, metadata={})

        result = handler.handle_before_agent(event)
        assert result.decision == "allow"

    def test_skill_resolution_uses_event_project(self) -> None:
        handler = _TestHandler()
        skill = MagicMock(name="project-only")
        skill.name = "project-only"
        handler._skill_manager.resolve_skill_name.return_value = skill
        event = _make_event(data={"prompt": "/gobby project-only"})
        event.project_id = "project-a"

        response = handler.handle_before_agent(event)

        assert response.context is not None
        handler._skill_manager.resolve_skill_name.assert_called_with(
            "project-only",
            project_id="project-a",
        )

    def test_updates_status_to_active(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )

        handler.handle_before_agent(event)
        handler._session_manager.update_session_status.assert_called_with(
            "sess-1",
            "active",
            activity_confirmed=True,
        )
        assert handler._session_manager.update_session_status.call_count >= 1
        assert handler._session_manager.update_session_status.call_args is not None

    def test_resets_subagent_count_at_start_of_parent_turn(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )

        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls:
            mock_svm = MagicMock()
            mock_svm_cls.return_value = mock_svm

            result = handler.handle_before_agent(event)

            assert result.decision == "allow"
            mock_svm.merge_variables.assert_any_call(
                "sess-1",
                {"subagent_count": 0, "is_subagent": False},
            )

    def test_clear_command_generates_summaries(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "/clear"},
            metadata={"_platform_session_id": "sess-1"},
        )

        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls:
            handler.handle_before_agent(event)

        handler._dispatch_session_summaries_fn.assert_called_once_with(
            "sess-1",
            False,
            None,
            False,
        )
        mock_svm_cls.return_value.set_variable.assert_not_called()
        assert handler._dispatch_session_summaries_fn.call_count == 1
        assert handler._dispatch_session_summaries_fn.call_args is not None

    def test_exit_command_generates_summaries(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "/exit"},
            metadata={"_platform_session_id": "sess-1"},
        )

        handler.handle_before_agent(event)
        handler._dispatch_session_summaries_fn.assert_called_once_with(
            "sess-1",
            False,
            None,
            False,
        )
        assert handler._dispatch_session_summaries_fn.call_count == 1
        assert handler._dispatch_session_summaries_fn.call_args is not None

    def test_skill_interception(self) -> None:
        handler = _TestHandler()
        handler._skill_manager.resolve_skill_name.return_value = None
        handler._skill_manager.match_triggers.return_value = []

        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_before_agent(event)
        assert result.decision == "allow"

    def test_default_agent_injects_full_preamble_without_active_skill_manifest(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )

        agent_path = files("gobby.install.shared").joinpath("workflows/agents/default.yaml")
        default_agent = AgentDefinitionBody.model_validate(
            yaml.safe_load(agent_path.read_text(encoding="utf-8"))
        )

        with (
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                return_value={
                    "_agent_type": "default",
                    "_active_skill_names": ["brevity"],
                    "_agent_context_injected": False,
                    "_agent_context_rehydrate_pending": True,
                },
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
            patch("gobby.workflows.agent_resolver.resolve_agent", return_value=default_agent),
        ):
            result = handler.handle_before_agent(event)

        assert result.decision == "allow"
        assert result.context is not None
        persona = default_agent.prompt_for("persona")
        assert persona is not None
        assert persona in result.context
        assert "## Role" in result.context
        assert "## Working Style" in result.context
        assert "## Platform Context" in result.context
        assert "end_agent_run" not in result.context
        assert "Think out loud" not in result.context
        assert "Show your reasoning" not in result.context
        assert "Be technically sharp, candid, concise, and curious." in persona
        assert "<active_skills>" not in result.context
        assert "### brevity" not in result.context
        mock_merge.assert_any_call(
            "sess-1",
            {
                "_agent_context_injected": True,
                "_agent_identity_reinject": False,
                "_agent_context_rehydrate_pending": False,
            },
        )

    def test_agent_preamble_injected_once_on_first_prompt(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )
        agent = AgentDefinitionBody(
            prompts={
                "persona": (
                    "## Role\nAct as the daemon.\n\n"
                    "## Goal\nKeep the session coherent.\n\n"
                    "## Personality\nDirect and technical.\n\n"
                    "## Instructions\nUse the session lifecycle correctly."
                ),
                "agent": "AUTOMATED AGENT PROMPT",
            },
            name="default",
            surfaces=["spawn", "persona"],
        )

        with (
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                side_effect=[
                    {
                        "_agent_type": "default",
                        "_agent_context_injected": False,
                        "_agent_context_rehydrate_pending": True,
                    },
                    {
                        "_agent_type": "default",
                        "_agent_context_injected": True,
                        "_agent_context_rehydrate_pending": False,
                    },
                ],
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
            patch("gobby.workflows.agent_resolver.resolve_agent", return_value=agent),
        ):
            first = handler.handle_before_agent(event)
            second = handler.handle_before_agent(event)

        assert first.context is not None
        assert first.context.count("## Role") == 1
        assert "## Goal\nKeep the session coherent." in first.context
        assert first.context.count("## Personality") == 1
        assert first.context.count("## Instructions") == 1
        assert second.context is None
        mock_merge.assert_any_call(
            "sess-1",
            {
                "_agent_context_injected": True,
                "_agent_identity_reinject": False,
                "_agent_context_rehydrate_pending": False,
            },
        )

    def test_spawned_hook_reinjects_agent_prompt_only(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            data={"prompt": "continue"},
            metadata={"_platform_session_id": "sess-1"},
        )
        agent = AgentDefinitionBody(
            name="backend-developer",
            surfaces=["spawn", "persona"],
            prompts={
                "persona": "PERSONA SENTINEL",
                "agent": "AGENT SENTINEL assigned_task_id end_agent_run",
            },
        )

        with (
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                return_value={
                    "_agent_type": "backend-developer",
                    "_persona_name": "qa-reviewer",
                    "_agent_context_injected": False,
                    "_agent_context_rehydrate_pending": True,
                    "is_spawned_agent": True,
                },
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager.merge_variables"),
            patch("gobby.workflows.agent_resolver.resolve_agent", return_value=agent),
        ):
            result = handler.handle_before_agent(event)

        assert result.context is not None
        assert "AGENT SENTINEL" in result.context
        assert "PERSONA SENTINEL" not in result.context

    def test_stale_agent_context_false_with_prior_activity_repairs_without_preamble(
        self,
    ) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        handler._session_manager.get.return_value = MagicMock(
            project_id="proj-1",
            message_count=186,
            turn_count=74,
        )
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )

        with (
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                return_value={
                    "_agent_type": "default",
                    "_agent_context_injected": False,
                    "_agent_context_rehydrate_pending": False,
                },
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
            patch("gobby.workflows.agent_resolver.resolve_agent") as mock_resolve_agent,
        ):
            result = handler.handle_before_agent(event)

        assert result.decision == "allow"
        assert result.context is None
        assert "## Role" not in (result.context or "")
        handler._session_manager.update_session_status.assert_called_once_with(
            "sess-1",
            "active",
            activity_confirmed=True,
        )
        handler._session_manager.reset_transcript_processed.assert_not_called()
        handler._session_manager.get.assert_called_once_with("sess-1")
        mock_resolve_agent.assert_not_called()
        mock_merge.assert_any_call("sess-1", {"_agent_context_injected": True})

    def test_persona_switch_reinjects_agent_preamble_once(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        handler._session_manager.get.return_value = MagicMock(
            project_id="proj-1",
            message_count=12,
            turn_count=5,
        )
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )
        agent = AgentDefinitionBody(
            prompts={
                "persona": (
                    "## Role\nAct as the operator.\n\n"
                    "## Goal\nKeep the persona current.\n\n"
                    "## Personality\nPrecise.\n\n"
                    "## Instructions\nUse the active persona."
                ),
                "agent": "AUTOMATED AGENT PROMPT",
            },
            name="operator",
            surfaces=["spawn", "persona"],
        )

        with (
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                side_effect=[
                    {
                        "_agent_type": "default",
                        "_persona_name": "operator",
                        "_agent_context_injected": True,
                        "_agent_identity_reinject": True,
                        "_agent_context_rehydrate_pending": False,
                    },
                    {
                        "_agent_type": "default",
                        "_persona_name": "operator",
                        "_agent_context_injected": True,
                        "_agent_identity_reinject": False,
                        "_agent_context_rehydrate_pending": False,
                    },
                ],
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
            patch(
                "gobby.workflows.agent_resolver.resolve_agent",
                return_value=agent,
            ) as mock_resolve,
        ):
            first = handler.handle_before_agent(event)
            second = handler.handle_before_agent(event)

        assert first.context is not None
        assert first.context.count("## Role") == 1
        assert "## Role\nAct as the operator." in first.context
        assert second.context is None
        mock_resolve.assert_called_once_with(
            "operator",
            handler._session_manager.db,
            project_id="proj-1",
        )
        assert mock_merge.call_args_list == [
            call("sess-1", {"subagent_count": 0, "is_subagent": False}),
            call(
                "sess-1",
                {
                    "_agent_context_injected": True,
                    "_agent_identity_reinject": False,
                    "_agent_context_rehydrate_pending": False,
                },
            ),
            call("sess-1", {"subagent_count": 0, "is_subagent": False}),
        ]

    def test_explicit_rehydrate_reinjects_agent_preamble_once(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        handler._session_manager.get.return_value = MagicMock(
            project_id="proj-1",
            message_count=20,
            turn_count=9,
        )
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )
        agent = AgentDefinitionBody(
            prompts={
                "persona": (
                    "## Role\nAct as the daemon.\n\n"
                    "## Goal\nRestore prompt context.\n\n"
                    "## Personality\nDirect.\n\n"
                    "## Instructions\nRehydrate after context loss."
                ),
                "agent": "AUTOMATED AGENT PROMPT",
            },
            name="default",
            surfaces=["spawn", "persona"],
        )

        with (
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                side_effect=[
                    {
                        "_agent_type": "default",
                        "_agent_context_injected": False,
                        "_agent_context_rehydrate_pending": True,
                    },
                    {
                        "_agent_type": "default",
                        "_agent_context_injected": True,
                        "_agent_context_rehydrate_pending": False,
                    },
                ],
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
            patch("gobby.workflows.agent_resolver.resolve_agent", return_value=agent),
        ):
            first = handler.handle_before_agent(event)
            second = handler.handle_before_agent(event)

        assert first.context is not None
        assert first.context.count("## Role") == 1
        assert "## Goal\nRestore prompt context." in first.context
        assert second.context is None
        assert mock_merge.call_args_list == [
            call("sess-1", {"subagent_count": 0, "is_subagent": False}),
            call(
                "sess-1",
                {
                    "_agent_context_injected": True,
                    "_agent_identity_reinject": False,
                    "_agent_context_rehydrate_pending": False,
                },
            ),
            call("sess-1", {"subagent_count": 0, "is_subagent": False}),
        ]


# ---------------------------------------------------------------------------
# _intercept_skill_command tests
# ---------------------------------------------------------------------------


class TestInterceptSkillCommand:
    """Tests for _intercept_skill_command."""

    def test_not_gobby_command(self) -> None:
        handler = _TestHandler()
        result = handler._intercept_skill_command("hello world")
        assert result is None

    def test_bare_gobby_returns_help(self) -> None:
        handler = _TestHandler()
        with patch.object(handler, "_generate_help_content", return_value="help text"):
            result = handler._intercept_skill_command("/gobby")
        assert result == "help text"

    def test_gobby_help(self) -> None:
        handler = _TestHandler()
        with patch.object(handler, "_generate_help_content", return_value="help text"):
            result = handler._intercept_skill_command("/gobby help")
        assert result == "help text"

    def test_codex_gobby_help_uses_codex_prefix(self) -> None:
        handler = _TestHandler()
        with patch.object(handler, "_generate_help_content", return_value="help text") as mock_help:
            result = handler._intercept_skill_command("$gobby help", "sess-1")

        assert result == "help text"
        mock_help.assert_called_once_with("sess-1", command_prefix="$gobby")

    def test_gobby_colon_skill(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.content = "# Expand skill"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby:expand")
        assert result is not None
        assert skill_fetch_directive("expand") in result
        assert "# Expand skill" not in result

    def test_gobby_space_skill(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.content = "# Expand"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby expand some args")
        assert result is not None
        assert skill_fetch_directive("expand") in result
        assert "User arguments:" not in result
        assert "some args" not in result

    def test_codex_gobby_space_skill(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.content = "# Expand"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("$gobby expand some args")

        assert result is not None
        assert skill_fetch_directive("expand") in result
        assert "User arguments:" not in result
        assert "some args" not in result

    def test_codex_gobby_coderabbit_multiline_paste_does_not_duplicate_args(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "coderabbit"
        mock_skill.content = "# CodeRabbit"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command(
            "$gobby coderabbit CodeRabbit finding 1\n"
            "Path: src/gobby/hooks/event_handlers/_agent.py\n"
            "Comment: duplicated clipboard text"
        )

        assert result is not None
        assert skill_fetch_directive("coderabbit") in result
        assert "User arguments:" not in result
        assert "CodeRabbit finding 1" not in result

    @pytest.mark.parametrize(
        "command",
        ["/gobby plan draft auth", "$gobby plan draft auth"],
    )
    def test_gobby_plan_does_not_inline_oversized_skill_body(self, command: str) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "plan"
        mock_skill.content = "# Plan\n" + ("x" * 20_000)
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command(command)

        assert result is not None
        assert skill_fetch_directive("plan") in result
        assert "User arguments:" not in result
        assert "draft auth" not in result
        assert "<skill-context" not in result
        assert "# Plan" not in result
        assert "... [truncated]" not in result

    def test_gobby_skill_not_found(self) -> None:
        handler = _TestHandler()
        handler._skill_manager.resolve_skill_name.return_value = None

        with patch.object(handler, "_skill_not_found_context", return_value="not found text"):
            result = handler._intercept_skill_command("/gobby:nonexistent")
        assert result == "not found text"

    def test_codex_gobby_skill_not_found_uses_codex_prefix(self) -> None:
        handler = _TestHandler()
        handler._skill_manager.resolve_skill_name.return_value = None

        with patch.object(
            handler, "_skill_not_found_context", return_value="not found text"
        ) as mock_not_found:
            result = handler._intercept_skill_command("$gobby nonexistent")

        assert result == "not found text"
        mock_not_found.assert_called_once_with("nonexistent", command_prefix="$gobby")

    def test_gobby_no_skill_manager(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None

        with pytest.raises(RuntimeError, match="skill_manager not initialized"):
            handler._intercept_skill_command("/gobby:expand")

    def test_gobby_colon_skill_with_args(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.content = "# Expand"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby:expand --tdd")
        assert result is not None
        assert "User arguments:" not in result
        assert "--tdd" not in result

    def test_gobby_skills_namespace(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "bridge"
        mock_skill.content = "# Bridge skill"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby skills bridge")
        assert result is not None
        assert skill_fetch_directive("bridge") in result
        assert "# Bridge skill" not in result
        handler._skill_manager.resolve_skill_name.assert_called_with("bridge")

    def test_gobby_skill_singular_namespace(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "bridge"
        mock_skill.content = "# Bridge skill"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby skill bridge")
        assert result is not None
        assert skill_fetch_directive("bridge") in result

    def test_gobby_skills_namespace_with_args(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "bridge"
        mock_skill.content = "# Bridge"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby skills bridge --verbose")
        assert result is not None
        assert "User arguments:" not in result
        assert "--verbose" not in result

    def test_gobby_skills_bare_returns_help(self) -> None:
        handler = _TestHandler()
        with patch.object(handler, "_generate_help_content", return_value="help text"):
            result = handler._intercept_skill_command("/gobby skills")
        assert result == "help text"


# ---------------------------------------------------------------------------
# _suggest_skills tests
# ---------------------------------------------------------------------------


class TestSuggestSkills:
    """Tests for _suggest_skills."""

    def test_slash_command_skipped(self) -> None:
        handler = _TestHandler()
        result = handler._suggest_skills("/gobby:expand")
        assert result is None

    def test_codex_command_skipped(self) -> None:
        handler = _TestHandler()
        result = handler._suggest_skills("$gobby expand")
        assert result is None

    def test_no_matches(self) -> None:
        handler = _TestHandler()
        handler._skill_manager.match_triggers.return_value = []
        result = handler._suggest_skills("write some code")
        assert result is None

    def test_strong_match(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "commit"
        handler._skill_manager.match_triggers.return_value = [(mock_skill, 0.9)]

        with patch(
            "gobby.hooks.event_handlers._agent._load_agent_prompt",
            return_value="hint text",
        ):
            result = handler._suggest_skills("commit my changes")
        assert result == "hint text"

    def test_no_skill_manager(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None

        with pytest.raises(RuntimeError, match="skill_manager not initialized"):
            handler._suggest_skills("test")


# ---------------------------------------------------------------------------
# _generate_help_content tests
# ---------------------------------------------------------------------------


class TestGenerateHelpContent:
    """Tests for _generate_help_content."""

    def test_generate_help(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.description = "Expand tasks. Into subtasks."
        mock_skill.is_always_apply.return_value = False
        handler._skill_manager.discover_core_skills.return_value = [mock_skill]

        with patch(
            "gobby.hooks.event_handlers._agent._load_agent_prompt",
            return_value="help content",
        ):
            result = handler._generate_help_content()
        assert result == "help content"

    def test_generate_help_lists_user_invoked_skills(self) -> None:
        handler = _TestHandler()
        expand_skill = MagicMock()
        expand_skill.name = "expand"
        expand_skill.description = "Expand tasks. Into subtasks."
        expand_skill.is_always_apply.return_value = False

        plan_skill = MagicMock()
        plan_skill.name = "plan"
        plan_skill.description = "Draft plans."
        plan_skill.is_always_apply.return_value = False

        handler._skill_manager.discover_core_skills.return_value = [
            plan_skill,
            expand_skill,
        ]

        with patch(
            "gobby.hooks.event_handlers._agent._load_agent_prompt",
            return_value="help",
        ) as mock_load:
            handler._generate_help_content()

        skills_list = mock_load.call_args.args[1]["skills_list"]
        assert "- `/gobby expand` — Expand tasks" in skills_list
        assert "- `/gobby plan` — Draft plans" in skills_list
        assert skills_list.index("/gobby expand") < skills_list.index("/gobby plan")

    def test_generate_help_uses_command_prefix(self) -> None:
        handler = _TestHandler()
        expand_skill = MagicMock()
        expand_skill.name = "expand"
        expand_skill.description = "Expand tasks."
        expand_skill.is_always_apply.return_value = False
        handler._skill_manager.discover_core_skills.return_value = [expand_skill]

        with patch(
            "gobby.hooks.event_handlers._agent._load_agent_prompt",
            return_value="help",
        ) as mock_load:
            handler._generate_help_content(command_prefix="$gobby")

        context = mock_load.call_args.args[1]
        assert context["command_prefix"] == "$gobby"
        assert "- `$gobby expand` — Expand tasks" in context["skills_list"]

    def test_generate_help_logs_active_skill_filter_failure(self) -> None:
        handler = _TestHandler()
        skill = MagicMock()
        skill.name = "expand"
        skill.description = "Expand tasks."
        skill.is_always_apply.return_value = False
        handler._skill_manager.discover_core_skills.return_value = [skill]

        with (
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls,
            patch(
                "gobby.hooks.event_handlers._agent._load_agent_prompt",
                return_value="help",
            ),
        ):
            mock_svm_cls.return_value.get_variables.side_effect = RuntimeError("database offline")
            result = handler._generate_help_content(session_id="sess-1")

        assert result == "help"
        handler.logger.warning.assert_called_once()
        assert "active skills" in handler.logger.warning.call_args.args[0]

    def test_generate_help_filters_always_apply(self) -> None:
        handler = _TestHandler()
        regular_skill = MagicMock()
        regular_skill.name = "expand"
        regular_skill.description = "Expand tasks."
        regular_skill.is_always_apply.return_value = False

        auto_skill = MagicMock()
        auto_skill.name = "auto-inject"
        auto_skill.is_always_apply.return_value = True

        handler._skill_manager.discover_core_skills.return_value = [
            regular_skill,
            auto_skill,
        ]

        with patch(
            "gobby.hooks.event_handlers._agent._load_agent_prompt",
            return_value="help",
        ) as mock_load:
            handler._generate_help_content()
            # skills_list should only contain the regular skill
            # skills_list is passed in the context dict (second positional arg)
            skills_list = mock_load.call_args.args[1]["skills_list"]
            assert "expand" in skills_list
            assert "auto-inject" not in skills_list

    def test_generate_help_filters_router_skill(self) -> None:
        handler = _TestHandler()
        regular_skill = MagicMock()
        regular_skill.name = "expand"
        regular_skill.description = "Expand tasks."
        regular_skill.is_always_apply.return_value = False

        router_skill = MagicMock()
        router_skill.name = "gobby"
        router_skill.description = "Router."
        router_skill.is_always_apply.return_value = False

        handler._skill_manager.discover_core_skills.return_value = [
            regular_skill,
            router_skill,
        ]

        with patch(
            "gobby.hooks.event_handlers._agent._load_agent_prompt",
            return_value="help",
        ) as mock_load:
            handler._generate_help_content()

        skills_list = mock_load.call_args.args[1]["skills_list"]
        assert "/gobby expand" in skills_list
        assert "/gobby gobby" not in skills_list

    def test_no_skill_manager(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None

        with pytest.raises(RuntimeError):
            handler._generate_help_content()


# ---------------------------------------------------------------------------
# _skill_not_found_context tests
# ---------------------------------------------------------------------------


class TestSkillNotFoundContext:
    """Tests for _skill_not_found_context."""

    def test_returns_not_found_message(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.is_always_apply.return_value = False
        handler._skill_manager.discover_core_skills.return_value = [mock_skill]

        with patch(
            "gobby.hooks.event_handlers._agent._load_agent_prompt",
            return_value="not found msg",
        ) as mock_load:
            result = handler._skill_not_found_context("expa", command_prefix="$gobby")
        assert result == "not found msg"
        assert mock_load.call_args.args[1]["command_prefix"] == "$gobby"

    def test_no_skill_manager(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None

        with pytest.raises(RuntimeError):
            handler._skill_not_found_context("test")


# ---------------------------------------------------------------------------
# handle_after_agent tests
# ---------------------------------------------------------------------------


class TestHandleAfterAgent:
    """Tests for handle_after_agent."""

    def test_with_session(self) -> None:
        handler = _TestHandler()
        handler._apply_debug_echo = MagicMock()
        event = _make_event(
            event_type=HookEventType.AFTER_AGENT,
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_after_agent(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_called_with(
            "sess-1",
            "paused",
            activity_confirmed=True,
        )
        handler._apply_debug_echo.assert_called_once_with(result)

    def test_without_session(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.AFTER_AGENT,
            metadata={},
        )

        result = handler.handle_after_agent(event)
        assert result.decision == "allow"


# ---------------------------------------------------------------------------
# handle_stop tests
# ---------------------------------------------------------------------------


class TestHandleStop:
    """Tests for handle_stop."""

    def test_with_session(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.STOP,
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_stop(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_called_with(
            "sess-1",
            "paused",
            activity_confirmed=True,
        )

    def test_without_session(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.STOP,
            metadata={},
        )

        result = handler.handle_stop(event)
        assert result.decision == "allow"


# ---------------------------------------------------------------------------
# handle_pre_compact tests
# ---------------------------------------------------------------------------


class TestHandlePreCompact:
    """Tests for handle_pre_compact."""

    def test_qwen_skipped(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.PRE_COMPACT,
            source=SessionSource.QWEN,
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_pre_compact(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_not_called()

    def test_manual_claude_updates_status(self) -> None:
        handler = _TestHandler()
        handler._dispatch_session_summaries_fn = MagicMock()
        event = _make_event(
            event_type=HookEventType.PRE_COMPACT,
            source=SessionSource.CLAUDE,
            metadata={"_platform_session_id": "sess-1"},
            data={"trigger": "manual"},
        )

        result = handler.handle_pre_compact(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_called_with("sess-1", "handoff_ready")
        handler._dispatch_session_summaries_fn.assert_called_once_with(
            "sess-1",
            False,
            None,
            False,
        )

    @pytest.mark.parametrize("trigger", ["auto", "clear"])
    def test_non_handoff_compact_trigger_summarizes_without_status(self, trigger: str) -> None:
        handler = _TestHandler()
        handler._dispatch_session_summaries_fn = MagicMock()
        event = _make_event(
            event_type=HookEventType.PRE_COMPACT,
            source=SessionSource.CLAUDE,
            metadata={"_platform_session_id": "sess-1"},
            data={"trigger": trigger},
        )

        result = handler.handle_pre_compact(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_not_called()
        handler._dispatch_session_summaries_fn.assert_called_once_with(
            "sess-1",
            False,
            None,
            False,
        )

    def test_no_session_id(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.PRE_COMPACT,
            source=SessionSource.CLAUDE,
            metadata={},
        )

        result = handler.handle_pre_compact(event)
        assert result.decision == "allow"


# ---------------------------------------------------------------------------
# handle_subagent_start / handle_subagent_stop tests
# ---------------------------------------------------------------------------


class TestSubagentEvents:
    """Tests for subagent event handlers."""

    def test_subagent_start(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_START,
            metadata={"_platform_session_id": "sess-1"},
            data={"agent_id": "a1", "subagent_id": "sa1"},
        )

        result = handler.handle_subagent_start(event)
        assert result.decision == "allow"
        cast(MagicMock, handler._session_manager.db.fetchone).assert_not_called()
        assert not hasattr(handler, "_pending_subagent_depths")

    def test_subagent_start_increments_count_and_derives_is_subagent(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_START,
            metadata={"_platform_session_id": "sess-1"},
            data={"agent_id": "a1", "subagent_id": "sa1"},
        )

        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls:
            mock_svm = MagicMock()
            mock_svm_cls.return_value = mock_svm

            result = handler.handle_subagent_start(event)

            assert result.decision == "allow"
            mock_svm.adjust_counter_and_derive_boolean.assert_called_once_with(
                "sess-1",
                "subagent_count",
                1,
                boolean_name="is_subagent",
            )

    def test_subagent_start_no_ids(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_START,
            metadata={},
            data={},
        )

        result = handler.handle_subagent_start(event)
        assert result.decision == "allow"

    def test_subagent_start_no_session_skips_variable(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_START,
            metadata={},
            data={},
        )

        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls:
            result = handler.handle_subagent_start(event)

            assert result.decision == "allow"
            mock_svm_cls.assert_not_called()

    def test_subagent_stop(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_STOP,
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_subagent_stop(event)
        assert result.decision == "allow"

    def test_subagent_stop_decrements_count_and_derives_is_subagent(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_STOP,
            metadata={"_platform_session_id": "sess-1"},
        )

        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls:
            mock_svm = MagicMock()
            mock_svm_cls.return_value = mock_svm

            result = handler.handle_subagent_stop(event)

            assert result.decision == "allow"
            mock_svm.adjust_counter_and_derive_boolean.assert_called_once_with(
                "sess-1",
                "subagent_count",
                -1,
                boolean_name="is_subagent",
            )

    def test_subagent_stop_no_session(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_STOP,
            metadata={},
        )

        result = handler.handle_subagent_stop(event)
        assert result.decision == "allow"

    def test_subagent_stop_no_session_skips_variable(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_STOP,
            metadata={},
        )

        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls:
            result = handler.handle_subagent_stop(event)

            assert result.decision == "allow"
            mock_svm_cls.assert_not_called()


# ---------------------------------------------------------------------------
# _GOBBY_CMD_PATTERN tests
# ---------------------------------------------------------------------------


class TestGobbyCommandPattern:
    """Tests for the command regex pattern."""

    def test_bare_gobby(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("/gobby")
        assert m is not None
        assert m.group(1) is None

    def test_bare_codex_gobby(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("$gobby")
        assert m is not None
        assert m.group(1) is None

    def test_gobby_colon_skill(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("/gobby:expand")
        assert m is not None
        assert m.group(1) == "expand"

    def test_gobby_space_skill(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("/gobby expand --tdd")
        assert m is not None
        assert m.group(1) is None
        assert "expand --tdd" in m.group(2)

    def test_codex_gobby_space_skill(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("$gobby expand --tdd")
        assert m is not None
        assert m.group(1) is None
        assert "expand --tdd" in m.group(2)

    def test_not_gobby(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("/other command")
        assert m is None

    def test_gobby_requires_command_boundary(self) -> None:
        assert _GOBBY_CMD_PATTERN.match("/gobbyfoo") is None
        assert _GOBBY_CMD_PATTERN.match("$gobbyfoo") is None


class TestCodexHandoffTitleHook:
    """Tests for immediate Codex handoff title seeding."""

    def test_updates_provisional_title_immediately(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        session_manager = cast(MagicMock, handler._session_manager)
        session_manager.get.return_value.title_source = "provisional"
        prompt = (
            "A previous agent produced the plan below to accomplish the user's task. "
            "Implement the plan in a fresh context. Treat the plan as the source of user intent, "
            "re-read files as needed, and carry the work through implementation and verification."
            "\n\n# Seed Handoff Title"
        )
        event = _make_event(
            source=SessionSource.CODEX,
            data={"prompt": prompt},
            metadata={"_platform_session_id": "sess-1"},
        )

        response = handler.handle_before_agent(event)

        assert response.decision == "allow"
        session_manager.update_title.assert_called_once_with(
            "sess-1",
            "Seed Handoff Title",
            title_source="handoff",
        )

    @pytest.mark.parametrize(
        ("source", "title_source", "prompt"),
        [
            (
                SessionSource.CLAUDE,
                "provisional",
                (
                    "A previous agent produced the plan below to accomplish the user's task. "
                    "Implement the plan in a fresh context. Treat the plan as the source of user "
                    "intent, re-read files as needed, and carry the work through implementation "
                    "and verification.\n\n# Other Provider"
                ),
            ),
            (SessionSource.CODEX, "manual", "ordinary prompt"),
            (SessionSource.CODEX, "llm", "ordinary prompt"),
            (SessionSource.CODEX, "provisional", "ordinary prompt"),
        ],
    )
    def test_misses_leave_title_unchanged(
        self,
        source: SessionSource,
        title_source: str,
        prompt: str,
    ) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        session_manager = cast(MagicMock, handler._session_manager)
        session_manager.get.return_value.title_source = title_source
        event = _make_event(
            source=source,
            data={"prompt": prompt},
            metadata={"_platform_session_id": "sess-1"},
        )

        response = handler.handle_before_agent(event)

        assert response.decision == "allow"
        session_manager.update_title.assert_not_called()

    def test_missing_platform_session_is_ignored(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        session_manager = cast(MagicMock, handler._session_manager)
        event = _make_event(
            source=SessionSource.CODEX,
            data={"prompt": "ordinary prompt"},
        )

        response = handler.handle_before_agent(event)

        assert response.decision == "allow"
        session_manager.update_title.assert_not_called()

    def test_missing_stored_session_is_ignored(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        session_manager = cast(MagicMock, handler._session_manager)
        session_manager.get.return_value = None
        prompt = (
            "A previous agent produced the plan below to accomplish the user's task. "
            "Implement the plan in a fresh context. Treat the plan as the source of user intent, "
            "re-read files as needed, and carry the work through implementation and verification."
            "\n\n# Missing Session"
        )
        event = _make_event(
            source=SessionSource.CODEX,
            data={"prompt": prompt},
            metadata={"_platform_session_id": "missing-session"},
        )

        response = handler.handle_before_agent(event)

        assert response.decision == "allow"
        session_manager.update_title.assert_not_called()

    def test_storage_failure_logs_and_fails_open(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        session_manager = cast(MagicMock, handler._session_manager)
        test_logger = cast(MagicMock, handler.logger)
        session_manager.get.return_value.title_source = "provisional"
        session_manager.update_title.side_effect = RuntimeError("storage unavailable")
        prompt = (
            "A previous agent produced the plan below to accomplish the user's task. "
            "Implement the plan in a fresh context. Treat the plan as the source of user intent, "
            "re-read files as needed, and carry the work through implementation and verification."
            "\n\n# Fail Open"
        )
        event = _make_event(
            source=SessionSource.CODEX,
            data={"prompt": prompt},
            metadata={"_platform_session_id": "sess-1"},
        )

        response = handler.handle_before_agent(event)

        assert response.decision == "allow"
        test_logger.warning.assert_any_call(
            "Failed to seed Codex handoff title: %s",
            session_manager.update_title.side_effect,
        )
