"""Tests for Gemini adapter event and tool mapping."""

import pytest

from gobby.hooks.events import HookEventType

pytestmark = pytest.mark.unit


class TestEventTypeMapping:
    """Tests for Gemini event type mapping."""

    @pytest.mark.parametrize(
        "gemini_type,expected_type",
        [
            ("SessionStart", HookEventType.SESSION_START),
            ("SessionEnd", HookEventType.SESSION_END),
            ("BeforeAgent", HookEventType.BEFORE_AGENT),
            ("AfterAgent", HookEventType.AFTER_AGENT),
            ("BeforeTool", HookEventType.BEFORE_TOOL),
            ("AfterTool", HookEventType.AFTER_TOOL),
            ("BeforeToolSelection", HookEventType.BEFORE_TOOL_SELECTION),
            ("BeforeModel", HookEventType.BEFORE_MODEL),
            ("AfterModel", HookEventType.AFTER_MODEL),
            ("PreCompress", HookEventType.PRE_COMPACT),
            ("Notification", HookEventType.NOTIFICATION),
        ],
    )
    def test_event_map_coverage(self, adapter, gemini_type, expected_type) -> None:
        """EVENT_MAP maps all Gemini hook types correctly."""
        assert adapter.EVENT_MAP[gemini_type] == expected_type

    def test_event_map_has_all_gemini_types(self, adapter) -> None:
        """EVENT_MAP contains exactly 11 Gemini hook types."""
        assert len(adapter.EVENT_MAP) == 11

    @pytest.mark.parametrize(
        "event_type_value,expected_gemini_name",
        [
            ("session_start", "SessionStart"),
            ("session_end", "SessionEnd"),
            ("before_agent", "BeforeAgent"),
            ("after_agent", "AfterAgent"),
            ("before_tool", "BeforeTool"),
            ("after_tool", "AfterTool"),
            ("before_tool_selection", "BeforeToolSelection"),
            ("before_model", "BeforeModel"),
            ("after_model", "AfterModel"),
            ("pre_compact", "PreCompress"),
            ("notification", "Notification"),
        ],
    )
    def test_hook_event_name_map_coverage(
        self, adapter, event_type_value, expected_gemini_name
    ) -> None:
        """HOOK_EVENT_NAME_MAP reverse maps all event types correctly."""
        assert adapter.HOOK_EVENT_NAME_MAP[event_type_value] == expected_gemini_name


class TestToolNameNormalization:
    """Tests for Gemini tool name normalization."""

    @pytest.mark.parametrize(
        "gemini_tool,expected_tool",
        [
            # Shell/Bash
            ("run_shell_command", "Bash"),
            ("RunShellCommand", "Bash"),
            ("ShellTool", "Bash"),
            # File read
            ("read_file", "Read"),
            ("ReadFile", "Read"),
            ("ReadFileTool", "Read"),
            # File write
            ("write_file", "Write"),
            ("WriteFile", "Write"),
            ("WriteFileTool", "Write"),
            # File edit
            ("edit_file", "Edit"),
            ("EditFile", "Edit"),
            ("EditFileTool", "Edit"),
            ("replace", "Edit"),
            ("Replace", "Edit"),
            ("ReplaceTool", "Edit"),
            # Search/Glob/Grep
            ("GlobTool", "Glob"),
            ("glob", "Glob"),
            ("GrepTool", "Grep"),
            ("grep", "Grep"),
            ("grep_search", "Grep"),
            ("search_file_content", "Grep"),
            ("SearchText", "Grep"),
            # Directory listing
            ("list_directory", "Ls"),
            ("ListDirectory", "Ls"),
            ("ls", "Ls"),
            # Web access
            ("web_fetch", "Fetch"),
            ("FetchTool", "Fetch"),
            # MCP tools (Gobby MCP server)
            ("call_tool", "mcp__gobby__call_tool"),
            ("list_mcp_servers", "mcp__gobby__list_mcp_servers"),
            ("list_tools", "mcp__gobby__list_tools"),
            ("get_tool_schema", "mcp__gobby__get_tool_schema"),
            ("search_tools", "mcp__gobby__search_tools"),
            ("recommend_tools", "mcp__gobby__recommend_tools"),
            # Skill and agent tools
            ("activate_skill", "Skill"),
            ("delegate_to_agent", "Task"),
        ],
    )
    def test_tool_map_coverage(self, adapter, gemini_tool, expected_tool) -> None:
        """TOOL_MAP normalizes all known Gemini tool names."""
        assert adapter.normalize_tool_name(gemini_tool) == expected_tool

    def test_unknown_tool_passes_through(self, adapter) -> None:
        """Unknown tool names pass through unchanged."""
        assert adapter.normalize_tool_name("CustomTool") == "CustomTool"
        assert adapter.normalize_tool_name("mcp_server_tool") == "mcp_server_tool"

    def test_empty_tool_name(self, adapter) -> None:
        """Empty tool name passes through unchanged."""
        assert adapter.normalize_tool_name("") == ""
