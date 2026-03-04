"""Tests for hooks/event_handlers/_agent.py — targeting uncovered lines."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers._agent import (
    _GOBBY_CMD_PATTERN,
    AgentEventHandlerMixin,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource

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


class _TestHandler(AgentEventHandlerMixin):
    """Concrete implementation with required attributes for testing."""

    def __init__(self) -> None:
        self.logger = MagicMock()
        self._session_manager = MagicMock()
        self._session_storage = MagicMock()
        self._session_coordinator = None
        self._message_processor = None
        self._task_manager = None
        self._workflow_handler = None
        self._workflow_config = None
        self._message_manager = None
        self._skill_manager = MagicMock()
        self._skills_config = None
        self._session_task_manager = None
        self._dispatch_session_summaries_fn = MagicMock()
        self._get_machine_id = MagicMock(return_value="machine-1")
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

    def test_updates_status_to_active(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "hello"},
            metadata={"_platform_session_id": "sess-1"},
        )

        handler.handle_before_agent(event)
        handler._session_manager.update_session_status.assert_called_with("sess-1", "active")

    def test_clear_command_generates_summaries(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "/clear"},
            metadata={"_platform_session_id": "sess-1"},
        )

        handler.handle_before_agent(event)
        handler._dispatch_session_summaries_fn.assert_called_once_with("sess-1", False, None)

    def test_exit_command_generates_summaries(self) -> None:
        handler = _TestHandler()
        handler._skill_manager = None
        event = _make_event(
            data={"prompt": "/exit"},
            metadata={"_platform_session_id": "sess-1"},
        )

        handler.handle_before_agent(event)
        handler._dispatch_session_summaries_fn.assert_called_once()

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

    def test_gobby_colon_skill(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.content = "# Expand skill"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby:expand")
        assert result is not None
        assert "expand" in result
        assert "# Expand skill" in result

    def test_gobby_space_skill(self) -> None:
        handler = _TestHandler()
        mock_skill = MagicMock()
        mock_skill.name = "expand"
        mock_skill.content = "# Expand"
        handler._skill_manager.resolve_skill_name.return_value = mock_skill

        result = handler._intercept_skill_command("/gobby expand some args")
        assert result is not None
        assert "some args" in result

    def test_gobby_skill_not_found(self) -> None:
        handler = _TestHandler()
        handler._skill_manager.resolve_skill_name.return_value = None

        with patch.object(handler, "_skill_not_found_context", return_value="not found text"):
            result = handler._intercept_skill_command("/gobby:nonexistent")
        assert result == "not found text"

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
        assert "User arguments: --tdd" in result


# ---------------------------------------------------------------------------
# _suggest_skills tests
# ---------------------------------------------------------------------------


class TestSuggestSkills:
    """Tests for _suggest_skills."""

    def test_slash_command_skipped(self) -> None:
        handler = _TestHandler()
        result = handler._suggest_skills("/gobby:expand")
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
        ):
            result = handler._skill_not_found_context("expa")
        assert result == "not found msg"

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
        event = _make_event(
            event_type=HookEventType.AFTER_AGENT,
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_after_agent(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_called_with("sess-1", "paused")

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
        handler._session_manager.update_session_status.assert_called_with("sess-1", "paused")

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

    def test_gemini_skipped(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.PRE_COMPACT,
            source=SessionSource.GEMINI,
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_pre_compact(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_not_called()

    def test_claude_updates_status(self) -> None:
        handler = _TestHandler()
        handler._dispatch_session_summaries_fn = MagicMock()
        event = _make_event(
            event_type=HookEventType.PRE_COMPACT,
            source=SessionSource.CLAUDE,
            metadata={"_platform_session_id": "sess-1"},
            data={"trigger": "auto"},
        )

        result = handler.handle_pre_compact(event)
        assert result.decision == "allow"
        handler._session_manager.update_session_status.assert_called_with("sess-1", "handoff_ready")
        handler._dispatch_session_summaries_fn.assert_called_once_with("sess-1", False, None)

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

    def test_subagent_start_no_ids(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_START,
            metadata={},
            data={},
        )

        result = handler.handle_subagent_start(event)
        assert result.decision == "allow"

    def test_subagent_stop(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_STOP,
            metadata={"_platform_session_id": "sess-1"},
        )

        result = handler.handle_subagent_stop(event)
        assert result.decision == "allow"

    def test_subagent_stop_no_session(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SUBAGENT_STOP,
            metadata={},
        )

        result = handler.handle_subagent_stop(event)
        assert result.decision == "allow"


# ---------------------------------------------------------------------------
# _GOBBY_CMD_PATTERN tests
# ---------------------------------------------------------------------------


class TestGobbyCommandPattern:
    """Tests for the command regex pattern."""

    def test_bare_gobby(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("/gobby")
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

    def test_not_gobby(self) -> None:
        m = _GOBBY_CMD_PATTERN.match("/other command")
        assert m is None
