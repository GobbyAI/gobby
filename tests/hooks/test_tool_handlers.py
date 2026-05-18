"""Tool handler and Skill-tool interception tests."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


class TestToolHandlers:
    """Test BEFORE_TOOL and AFTER_TOOL handlers."""

    def test_before_tool_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_TOOL allows by default."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)
        assert response.decision == "allow"

    def test_after_tool_allows(self, event_handlers: EventHandlers) -> None:
        """Test AFTER_TOOL allows by default."""
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_after_tool(event)
        assert response.decision == "allow"

    def test_before_tool_allows_gobby_tasks_cli_dict_input(
        self, event_handlers: EventHandlers
    ) -> None:
        """Task CLI policy is enforced by rules, not hardcoded hook logic."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Bash",
                "tool_input": {"command": "uv run gobby tasks list --ready"},
            },
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_before_tool_allows_gobby_tasks_cli_string_input(
        self, event_handlers: EventHandlers
    ) -> None:
        """String shell payloads from app-server adapters are allowed by the hook."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": "gobby tasks list --limit 1"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_before_tool_allows_gobby_tasks_cli_exec_command_alias(
        self, event_handlers: EventHandlers
    ) -> None:
        """Shell aliases are left to the rules engine."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "exec_command", "tool_input": {"command": "gobby tasks list"}},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_before_tool_allows_other_gobby_cli_commands(
        self, event_handlers: EventHandlers
    ) -> None:
        """Other gobby CLI commands remain allowed."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": {"command": "uv run gobby status"}},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)

        assert response.decision == "allow"


class TestToolHandlerEdgeCases:
    """Test BEFORE_TOOL and AFTER_TOOL edge cases."""

    def test_before_tool_no_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_TOOL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
            metadata={},
        )

        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_after_tool_failure_status(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL handles is_failure metadata."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Write"},
            metadata={"_platform_session_id": "sess-123", "is_failure": True},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_after_tool_no_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            metadata={},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_after_tool_edit_marks_had_edits(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL marks had_edits for edit tools on regular files."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "/path/to/regular/file.py"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_called_once_with("sess-123")
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 1
        assert mock_dependencies["session_storage"].mark_had_edits.call_args is not None

    def test_after_tool_edit_marks_had_edits_for_in_repo_path(
        self, mock_dependencies: dict
    ) -> None:
        """Test AFTER_TOOL marks had_edits when the edited path resolves inside cwd."""
        repo_root = Path("/tmp/project")
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "src/regular.py"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(repo_root)

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_called_once_with("sess-123")
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 1
        assert mock_dependencies["session_storage"].mark_had_edits.call_args is not None

    def test_after_tool_notifies_code_index_with_project_root_path(
        self, mock_dependencies: dict, tmp_path: Path
    ) -> None:
        """Test code index notification uses project root even when cwd is nested."""
        repo_root = tmp_path / "project"
        deep_cwd = repo_root / "src" / "pkg"
        (repo_root / ".gobby").mkdir(parents=True)
        (repo_root / ".gobby" / "project.json").write_text('{"id": "proj-1"}')
        deep_cwd.mkdir(parents=True)
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        code_index_trigger = MagicMock()
        resolve_project_id = MagicMock(return_value="proj-1")
        handlers = EventHandlers(
            **mock_dependencies,
            code_index_trigger=code_index_trigger,
            resolve_project_id=resolve_project_id,
        )
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "edited.py"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(deep_cwd)

        handlers.handle_after_tool(event)

        resolve_project_id.assert_called_once_with(None, str(repo_root.resolve()))
        assert resolve_project_id.call_count == 1
        assert resolve_project_id.call_args is not None
        code_index_trigger.notify_file_changed.assert_called_once_with(
            file_path="src/pkg/edited.py",
            project_id="proj-1",
            root_path=str(repo_root.resolve()),
        )
        assert code_index_trigger.notify_file_changed.call_count == 1
        assert code_index_trigger.notify_file_changed.call_args is not None

    def test_after_tool_edit_skips_gobby_internal_files(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for .gobby/ internal files."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "/path/to/project/.gobby/tasks.jsonl"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 0
        assert not mock_dependencies["session_storage"].mark_had_edits.called

    def test_after_tool_edit_skips_out_of_repo_paths(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for edits outside cwd."""
        repo_root = Path("/tmp/project")
        mock_dependencies["task_manager"].list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "../outside/settings.json"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        event.cwd = str(repo_root)

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 0
        assert not mock_dependencies["session_storage"].mark_had_edits.called

    def test_after_tool_edit_skips_relative_gobby_path(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for relative .gobby/ paths."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Edit",
                "tool_input": {"file_path": ".gobby/memories.jsonl"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()
        assert mock_dependencies["session_storage"].mark_had_edits.call_count == 0
        assert not mock_dependencies["session_storage"].mark_had_edits.called


class TestSkillToolInterception:
    """Tests for Skill tool call interception in handle_before_tool."""

    @pytest.fixture
    def parsed_skill(self) -> Any:
        """Create a mock ParsedSkill for testing."""
        from gobby.skills.parser import ParsedSkill

        return ParsedSkill(
            name="agent-monitoring",
            description="Inspect Gobby agent progress through supported MCP tools.",
            content="# Agent Monitoring\nInspect agent progress.",
        )

    @pytest.fixture
    def skill_manager(self, parsed_skill: Any) -> MagicMock:
        """Create a mock skill manager that resolves agent-monitoring."""
        manager = MagicMock()
        manager.resolve_skill_name.return_value = parsed_skill
        return manager

    @pytest.fixture
    def handlers_with_skills(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> EventHandlers:
        """EventHandlers with a skill manager configured."""
        mock_dependencies["skill_manager"] = skill_manager
        return EventHandlers(**mock_dependencies)

    def test_skill_tool_resolves_gobby_skill(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Skill tool call with a gobby skill name blocks with fetch directive."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "agent-monitoring"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "block"
        assert 'Call get_skill(name="agent-monitoring") on gobby-skills, then continue.' in (
            response.context or ""
        )
        assert "# Agent Monitoring" not in (response.context or "")
        assert "<skill-context" not in (response.context or "")
        skill_manager.resolve_skill_name.assert_called_once_with("agent-monitoring")

    def test_skill_tool_with_gobby_prefix(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Skill tool call with gobby: prefix strips it before resolving."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "gobby:agent-monitoring"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "block"
        assert 'Call get_skill(name="agent-monitoring") on gobby-skills, then continue.' in (
            response.context or ""
        )
        skill_manager.resolve_skill_name.assert_called_once_with("agent-monitoring")

    def test_skill_tool_with_args(self, handlers_with_skills: EventHandlers) -> None:
        """Skill tool call with args includes them in context."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "Skill",
                "tool_input": {"skill": "agent-monitoring", "args": "status"},
            },
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "block"
        assert "User arguments: status" in response.context

    def test_skill_tool_unknown_allows_native_handler(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Unknown Skill names pass through to the native handler."""
        skill_manager.resolve_skill_name.return_value = None
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "unknown-thing"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        skill_manager.resolve_skill_name.assert_called_once_with("unknown-thing")

    def test_skill_tool_non_gobby_namespace(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Skill tool call with non-gobby namespace is not intercepted."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "ms-office-suite:pdf"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        skill_manager.resolve_skill_name.assert_not_called()

    def test_non_skill_tool_unaffected(
        self, handlers_with_skills: EventHandlers, skill_manager: MagicMock
    ) -> None:
        """Non-Skill tool calls are unaffected."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": {"command": "ls"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        skill_manager.resolve_skill_name.assert_not_called()

    def test_skill_tool_no_skill_manager(self, mock_dependencies: dict[str, Any]) -> None:
        """Skill tool call without skill_manager passes through."""
        handlers = EventHandlers(**mock_dependencies)  # no skill_manager
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "agent-monitoring"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_skill_tool_programming_error_propagates(
        self,
        handlers_with_skills: EventHandlers,
        skill_manager: MagicMock,
    ) -> None:
        """Programming errors during skill resolution are not swallowed."""
        skill_manager.resolve_skill_name.side_effect = RuntimeError("boom")
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "agent-monitoring"}},
        )

        with pytest.raises(RuntimeError, match="boom"):
            handlers_with_skills.handle_before_tool(event)

    def test_skill_tool_resolution_failure_allows_native_handler(
        self,
        handlers_with_skills: EventHandlers,
        skill_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Expected resolution failures are logged and fall through."""
        skill_manager.resolve_skill_name.side_effect = ValueError("temporary miss")
        caplog.set_level(logging.WARNING)
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "agent-monitoring"}},
        )
        response = handlers_with_skills.handle_before_tool(event)

        assert response.decision == "allow"
        assert "Failed to resolve Skill tool call" in caplog.text
        assert any(record.exc_info is not None for record in caplog.records)

    def test_skill_tool_tier2_mcp_fallback(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> None:
        """Tier 2: When local resolve fails, falls back to gobby-skills MCP get_skill."""
        skill_manager.resolve_skill_name.return_value = None
        mock_call_tool = MagicMock(
            return_value={
                "success": True,
                "skill": {"name": "playwright", "content": "# Playwright\nBrowser automation."},
            }
        )
        mock_dependencies["skill_manager"] = skill_manager
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "playwright"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "block"
        assert 'Call get_skill(name="playwright") on gobby-skills, then continue.' in (
            response.context or ""
        )
        assert "Browser automation" not in (response.context or "")
        assert "<skill-context" not in (response.context or "")
        mock_call_tool.assert_any_call("gobby-skills", "get_skill", {"name": "playwright"})

    def test_skill_tool_hub_match_not_searched_for_native_loop(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> None:
        """Hub-only matches are not searched; native Skill names pass through."""
        skill_manager.resolve_skill_name.return_value = None

        def _mock_call(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            if tool == "get_skill":
                return {"success": False}
            if tool == "search_hub":
                return {
                    "success": True,
                    "results": [
                        {
                            "display_name": "playwright-cli",
                            "slug": "playwright-cli",
                            "description": "Browser automation via Playwright",
                            "hub_name": "clawdhub",
                        }
                    ],
                }
            return {"success": False}

        mock_call_tool = MagicMock(side_effect=_mock_call)
        mock_dependencies["skill_manager"] = skill_manager
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "/loop"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"
        mock_call_tool.assert_called_once_with("gobby-skills", "get_skill", {"name": "/loop"})
        assert all(call.args[1] != "search_hub" for call in mock_call_tool.call_args_list)

    def test_skill_tool_unresolved_name_allows_native_handler(
        self, mock_dependencies: dict[str, Any], skill_manager: MagicMock
    ) -> None:
        """Unresolved names pass through after local and MCP misses."""
        skill_manager.resolve_skill_name.return_value = None

        def _mock_call(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": False}

        mock_call_tool = MagicMock(side_effect=_mock_call)
        mock_dependencies["skill_manager"] = skill_manager
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "nonexistent"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"
        mock_call_tool.assert_called_once_with("gobby-skills", "get_skill", {"name": "nonexistent"})

    def test_skill_tool_no_manager_but_has_call_tool(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """Without skill_manager but with call_tool, tier 2 still works."""
        mock_call_tool = MagicMock(
            return_value={
                "success": True,
                "skill": {"name": "playwright", "content": "# Playwright skill"},
            }
        )
        mock_dependencies["call_tool"] = mock_call_tool
        handlers = EventHandlers(**mock_dependencies)  # no skill_manager

        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Skill", "tool_input": {"skill": "playwright"}},
        )
        response = handlers.handle_before_tool(event)

        assert response.decision == "block"
        assert 'Call get_skill(name="playwright") on gobby-skills, then continue.' in (
            response.context or ""
        )
