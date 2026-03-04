"""
Tests for TranscriptAnalyzer in gobby.sessions.analyzer.
"""

from unittest.mock import Mock

import pytest

from gobby.sessions.analyzer import HandoffContext, TranscriptAnalyzer
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

pytestmark = pytest.mark.unit


# Note: TestHandoffContextActiveSkills removed - active_skills field was removed from HandoffContext
# as it was redundant with _build_skill_injection_context() which handles skill restoration on session start


@pytest.fixture
def sample_turns():
    return [
        {"type": "user", "message": {"content": "Fix the bug in the login page"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Okay doing it"},
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "get_task",
                            "arguments": {"task_id": "gt-123"},
                        },
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/path/to/login.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git commit -m 'fix login'"},
                    },
                ]
            },
        },
    ]


def test_extract_handoff_context_basic(sample_turns) -> None:
    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(sample_turns)

    assert ctx.initial_goal == "Fix the bug in the login page"
    assert ctx.active_gobby_task is not None
    assert ctx.active_gobby_task["id"] == "gt-123"
    assert "/path/to/login.py" in ctx.files_modified
    assert len(ctx.git_commits) == 1
    assert "git commit -m 'fix login'" in ctx.git_commits[0]["command"]


def test_extract_handoff_context_empty() -> None:
    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context([])
    assert ctx.initial_goal == ""
    assert not ctx.active_gobby_task
    assert not ctx.files_modified


def test_extract_handoff_context_no_task(sample_turns) -> None:
    # Filter out mcp_call_tool lines
    # This requires deep filtering of the fixture structure
    turns = [
        {"type": "user", "message": {"content": "Fix the bug"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/path/to/login.py"},
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)
    assert ctx.active_gobby_task is None
    assert "/path/to/login.py" in ctx.files_modified


def test_extract_handoff_context_recent_activity(sample_turns) -> None:
    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(sample_turns)
    # Check that recent tools are captured with detailed descriptions
    # The fixture has mcp_call_tool, Write, Bash (reverse order: Bash, Write, mcp_call_tool)
    assert len(ctx.recent_activity) > 0
    # New format shows details: "Ran: <cmd>", "Write: <path>", "Called <server>.<tool>"
    activity_str = " ".join(ctx.recent_activity)
    assert "Ran:" in activity_str or "Write:" in activity_str


def test_extract_handoff_context_max_turns() -> None:
    # Generate many turns
    turns = [{"type": "user", "message": {"content": f"msg {i}"}} for i in range(200)]
    # Make the last one have a goal just to check scanning logic (first user message is usually goal)
    # But max_turns applies to backward scan for activity.
    # Initial goal scan is forward from 0.

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns, max_turns=50)

    assert ctx.initial_goal == "msg 0"
    # recent_activity should only scan last 50 turns?
    # Logic says `relevant_turns = turns[-max_turns:]`
    # So it won't see tool uses before that.


def test_extract_handoff_context_max_turns_deprecated() -> None:
    """Test that max_turns parameter is accepted but ignored (deprecated)."""
    import inspect

    from gobby.sessions.analyzer import TranscriptAnalyzer

    # max_turns still in signature for backward compat, defaults to None
    sig = inspect.signature(TranscriptAnalyzer.extract_handoff_context)
    max_turns_param = sig.parameters["max_turns"]
    assert max_turns_param.default is None, "Default max_turns should be None (deprecated)"


def test_extract_handoff_context_processes_all_turns() -> None:
    """Test that all turns are processed regardless of max_turns."""
    # Create turns with file modifications at different positions
    turns = []
    for i in range(200):
        turns.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": f"/path/file{i}.py"},
                        },
                    ]
                },
            }
        )

    analyzer = TranscriptAnalyzer()

    # Even with max_turns=50 passed, all turns should be processed
    ctx = analyzer.extract_handoff_context(turns, max_turns=50)
    # Should have files from ALL turns, not just the last 50
    assert "/path/file199.py" in ctx.files_modified
    assert "/path/file0.py" in ctx.files_modified
    assert len(ctx.files_modified) == 200


def test_malformed_tool_blocks_not_dict() -> None:
    """Test handling of malformed content blocks that aren't dicts."""
    turns = [
        {"type": "user", "message": {"content": "do something"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    "plain string block",  # Not a dict
                    123,  # Integer
                    None,  # None
                    {"type": "text", "text": "valid text block"},
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/valid.py"}},
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # Should still extract the valid Write call
    assert "/valid.py" in ctx.files_modified


def test_malformed_tool_blocks_missing_keys() -> None:
    """Test handling of tool_use blocks missing expected keys."""
    turns = [
        {"type": "user", "message": {"content": "do something"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use"},  # Missing name and input
                    {"type": "tool_use", "name": "Edit"},  # Missing input
                    {"type": "tool_use", "name": "Write", "input": {}},  # Missing file_path
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/good.py"}},
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # Should only extract the one with valid file_path
    assert ctx.files_modified == ["/good.py"]


def test_multiple_edit_write_calls() -> None:
    """Test extraction of multiple Edit and Write calls to different files."""
    turns = [
        {"type": "user", "message": {"content": "refactor the code"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/foo.py"}},
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/src/bar.py"}},
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/baz.py"}},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/src/foo.py"},
                    },  # Duplicate
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # Should have 3 unique files, sorted
    assert ctx.files_modified == ["/src/bar.py", "/src/baz.py", "/src/foo.py"]


def test_git_status_not_extracted_from_transcript() -> None:
    """Test that git_status remains empty from transcript (enriched separately)."""
    turns = [
        {"type": "user", "message": {"content": "check status"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git status"},
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # git_status should remain empty - it's enriched by the caller, not extracted
    assert ctx.git_status == ""


def test_multiple_git_commits() -> None:
    """Test extraction of multiple git commit commands."""
    turns = [
        {"type": "user", "message": {"content": "commit the changes"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git add . && git commit -m 'first commit'"},
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git commit -m 'second commit'"},
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert len(ctx.git_commits) == 2
    # Commits are in reverse order (latest first) due to reverse iteration
    assert "second commit" in ctx.git_commits[0]["command"]
    assert "first commit" in ctx.git_commits[1]["command"]


def test_large_transcript_max_turns_limits_scanning() -> None:
    """Test that max_turns limits which turns are scanned for activity."""
    # Create a large transcript with 150 turns
    turns = []

    # First turn is the goal
    turns.append({"type": "user", "message": {"content": "Original goal"}})

    # Add 100 turns with tool uses
    for i in range(100):
        turns.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": f"/file_{i}.py"},
                        },
                    ]
                },
            }
        )

    # Add 50 more turns at the end
    for i in range(100, 150):
        turns.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": f"/late_{i}.py"},
                        },
                    ]
                },
            }
        )

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns, max_turns=50)

    # Should still get the initial goal from beginning
    assert ctx.initial_goal == "Original goal"

    # All turns are now processed (max_turns is deprecated/ignored)
    # Should have files from both early and late turns
    assert any(f.startswith("/late_") for f in ctx.files_modified)
    assert any(f.startswith("/file_") for f in ctx.files_modified)
    assert len(ctx.files_modified) == 150  # 100 early + 50 late


def test_content_as_string_not_list() -> None:
    """Test handling when message content is a string instead of list."""
    turns = [
        {"type": "user", "message": {"content": "plain string user message"}},
        {"type": "assistant", "message": {"content": "plain string assistant message"}},
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # Should handle gracefully without errors
    assert ctx.initial_goal == "plain string user message"
    assert ctx.files_modified == []


def test_recent_activity_limited_to_10() -> None:
    """Test that recent_activity is limited to last 10 tool calls."""
    turns = []
    for i in range(20):
        turns.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": f"Tool{i}", "input": {}},
                    ]
                },
            }
        )

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert len(ctx.recent_activity) == 10
    # Should be the last 10 (reverse order: 19, 18, ..., 10)
    assert "Called Tool19" in ctx.recent_activity[0]
    assert "Called Tool10" in ctx.recent_activity[9]
    assert "Called Tool15" in ctx.recent_activity[4]


def test_mcp_call_tool_gobby_tasks_extracts_task() -> None:
    """Test that mcp_call_tool with gobby-tasks extracts active task."""
    turns = [
        {"type": "user", "message": {"content": "work on the task"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "update_task",
                            "arguments": {"task_id": "gt-abc123", "status": "in_progress"},
                        },
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert ctx.active_gobby_task is not None
    assert ctx.active_gobby_task["id"] == "gt-abc123"
    assert ctx.active_gobby_task["action"] == "update_task"


def test_gobby_tasks_without_task_id() -> None:
    """Test gobby-tasks calls without task_id don't set active_gobby_task."""
    turns = [
        {"type": "user", "message": {"content": "list tasks"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "list_tasks",
                            "arguments": {},  # No task_id
                        },
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # Should not set active task since no task_id was provided
    assert ctx.active_gobby_task is None


def test_gobby_tasks_uses_id_field() -> None:
    """Test gobby-tasks calls with 'id' instead of 'task_id' are recognized."""
    turns = [
        {"type": "user", "message": {"content": "work on task"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "get_task",
                            "arguments": {"id": "gt-def456"},  # Using 'id' instead of 'task_id'
                        },
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert ctx.active_gobby_task is not None
    assert ctx.active_gobby_task["id"] == "gt-def456"


def test_gobby_tasks_only_first_task_captured() -> None:
    """Test that only the most recent (first in reverse) task is captured."""
    # Since we iterate in reverse, the "latest" turn comes first
    # and sets active_gobby_task. Subsequent turns shouldn't overwrite it.
    turns = [
        {"type": "user", "message": {"content": "work on tasks"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "get_task",
                            "arguments": {"task_id": "gt-first"},
                        },
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "update_task",
                            "arguments": {"task_id": "gt-latest"},
                        },
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # Should get the latest task (gt-latest) since we iterate in reverse
    assert ctx.active_gobby_task is not None
    assert ctx.active_gobby_task["id"] == "gt-latest"


def test_gobby_tasks_with_title() -> None:
    """Test gobby-tasks calls with title include it in the active task."""
    turns = [
        {"type": "user", "message": {"content": "create task"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": "create_task",
                            "arguments": {"task_id": "gt-new", "title": "Fix the login bug"},
                        },
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert ctx.active_gobby_task is not None
    assert ctx.active_gobby_task["title"] == "Fix the login bug"


def test_non_gobby_tasks_mcp_calls() -> None:
    """Test that MCP calls to other servers don't affect active_gobby_task."""
    turns = [
        {"type": "user", "message": {"content": "do something"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp_call_tool",
                        "input": {
                            "server_name": "gobby-memory",  # Not gobby-tasks
                            "tool_name": "create_memory",
                            "arguments": {"key": "value"},
                        },
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    # Should not set active task
    assert ctx.active_gobby_task is None


def test_alternative_file_path_keys() -> None:
    """Test that alternative file path keys are recognized (TargetFile, path)."""
    turns = [
        {"type": "user", "message": {"content": "modify files"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    # Standard file_path
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a.py"}},
                    # Alternative TargetFile (Antigravity-style)
                    {
                        "type": "tool_use",
                        "name": "replace_file_content",
                        "input": {"TargetFile": "/b.py"},
                    },
                    # Alternative path
                    {"type": "tool_use", "name": "write_to_file", "input": {"path": "/c.py"}},
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert "/a.py" in ctx.files_modified
    assert "/b.py" in ctx.files_modified
    assert "/c.py" in ctx.files_modified


class TestFormatToolDescription:
    """Tests for _format_tool_description method."""

    def test_mcp_call_tool(self) -> None:
        """Test gobby-tasks create_task shows enhanced format with title."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "Fix the bug"},
            },
        }
        assert analyzer._format_tool_description(block) == "Created task: Fix the bug"

    def test_mcp_call_tool_create_task_with_parent(self) -> None:
        """Test gobby-tasks create_task shows parent when provided."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "Subtask", "parent_task_id": "#123"},
            },
        }
        assert analyzer._format_tool_description(block) == "Created task: Subtask (parent: #123)"

    def test_mcp_call_tool_create_task_no_title(self) -> None:
        """Test gobby-tasks create_task defaults to 'Untitled' when no title."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {"server_name": "gobby-tasks", "tool_name": "create_task"},
        }
        assert analyzer._format_tool_description(block) == "Created task: Untitled"

    def test_mcp_call_tool_update_task_status(self) -> None:
        """Test gobby-tasks update_task shows status change."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "gobby-tasks",
                "tool_name": "update_task",
                "arguments": {"task_id": "#456", "status": "in_progress"},
            },
        }
        assert analyzer._format_tool_description(block) == "Updated task #456: status → in_progress"

    def test_mcp_call_tool_close_task(self) -> None:
        """Test gobby-tasks close_task shows reason."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#789", "reason": "Completed implementation"},
            },
        }
        assert (
            analyzer._format_tool_description(block) == "Closed task #789: Completed implementation"
        )

    def test_mcp_call_tool_claim_task(self) -> None:
        """Test gobby-tasks claim_task shows task ID."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "#100"},
            },
        }
        assert analyzer._format_tool_description(block) == "Claimed task #100"

    def test_mcp_call_tool_alternative_name(self) -> None:
        """Test alternative MCP tool name format for non-gobby-tasks servers."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp_call_tool",
            "input": {"server_name": "context7", "tool_name": "get_docs"},
        }
        assert analyzer._format_tool_description(block) == "Called context7.get_docs"

    def test_mcp_call_tool_generic_with_query(self) -> None:
        """Test generic MCP calls extract query argument."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "context7",
                "tool_name": "search_docs",
                "arguments": {"query": "how to use websockets"},
            },
        }
        assert (
            analyzer._format_tool_description(block)
            == "context7.search_docs: how to use websockets"
        )

    def test_mcp_call_tool_generic_with_title(self) -> None:
        """Test generic MCP calls extract title argument."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "some-server",
                "tool_name": "create_item",
                "arguments": {"title": "New Feature Request"},
            },
        }
        assert (
            analyzer._format_tool_description(block)
            == "some-server.create_item: New Feature Request"
        )

    def test_mcp_call_tool_generic_with_path(self) -> None:
        """Test generic MCP calls extract path argument."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "filesystem",
                "tool_name": "read_file",
                "arguments": {"path": "/etc/config.yaml"},
            },
        }
        assert analyzer._format_tool_description(block) == "filesystem.read_file: /etc/config.yaml"

    def test_mcp_call_tool_generic_truncates_long_values(self) -> None:
        """Test generic MCP calls truncate long argument values."""
        analyzer = TranscriptAnalyzer()
        long_query = "a" * 120  # 120 chars, should be truncated to 100
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "search",
                "tool_name": "find",
                "arguments": {"query": long_query},
            },
        }
        result = analyzer._format_tool_description(block)
        assert result.startswith("search.find: ")
        assert result.endswith("...")
        assert len(result) <= 120  # "search.find: " + 100 chars max

    def test_mcp_call_tool_generic_priority_order(self) -> None:
        """Test generic MCP calls use priority order (query > title > path)."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "test",
                "tool_name": "mixed",
                "arguments": {
                    "path": "/some/path",
                    "title": "Some Title",
                    "query": "search term",  # Should win (highest priority)
                },
            },
        }
        assert analyzer._format_tool_description(block) == "test.mixed: search term"

    def test_mcp_call_tool_generic_ignores_session_id(self) -> None:
        """Test generic MCP calls ignore session_id in fallback."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp__gobby__call_tool",
            "input": {
                "server_name": "test",
                "tool_name": "do_thing",
                "arguments": {"session_id": "abc123"},  # Should be ignored
            },
        }
        # No meaningful context extracted, falls back to basic format
        assert analyzer._format_tool_description(block) == "Called test.do_thing"

    def test_bash_command(self) -> None:
        """Test Bash commands show the actual command."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Bash", "input": {"command": "git status"}}
        assert analyzer._format_tool_description(block) == "Ran: git status"

    def test_bash_long_command_truncated(self) -> None:
        """Test long Bash commands are truncated."""
        analyzer = TranscriptAnalyzer()
        long_cmd = "git log --oneline --graph --all --decorate | head -50 && git status"
        block = {"name": "Bash", "input": {"command": long_cmd}}
        result = analyzer._format_tool_description(block)
        assert result.startswith("Ran: ")
        assert result.endswith("...")
        assert len(result) <= 65  # "Ran: " + 57 chars + "..."

    def test_edit_with_path(self) -> None:
        """Test Edit shows file path."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Edit", "input": {"file_path": "/src/main.py"}}
        assert analyzer._format_tool_description(block) == "Edit: /src/main.py"

    def test_write_with_path(self) -> None:
        """Test Write shows file path."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Write", "input": {"file_path": "/new_file.py"}}
        assert analyzer._format_tool_description(block) == "Write: /new_file.py"

    def test_read_with_path(self) -> None:
        """Test Read shows file path."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Read", "input": {"file_path": "/config.yaml"}}
        assert analyzer._format_tool_description(block) == "Read: /config.yaml"

    def test_glob_with_pattern(self) -> None:
        """Test Glob shows pattern."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Glob", "input": {"pattern": "**/*.py"}}
        assert analyzer._format_tool_description(block) == "Glob: **/*.py"

    def test_grep_with_pattern(self) -> None:
        """Test Grep shows pattern."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Grep", "input": {"pattern": "def test_"}}
        assert analyzer._format_tool_description(block) == "Grep: def test_"

    def test_grep_long_pattern_truncated(self) -> None:
        """Test long Grep patterns are truncated."""
        analyzer = TranscriptAnalyzer()
        long_pattern = "some_very_long_pattern_that_exceeds_the_limit_for_display"
        block = {"name": "Grep", "input": {"pattern": long_pattern}}
        result = analyzer._format_tool_description(block)
        assert result.startswith("Grep: ")
        assert result.endswith("...")
        assert len(result) <= 47  # "Grep: " + 37 chars + "..."

    def test_task_with_subagent(self) -> None:
        """Test Task shows subagent type and description."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "Task",
            "input": {"subagent_type": "Explore", "description": "Find auth code"},
        }
        assert analyzer._format_tool_description(block) == "Task (Explore): Find auth code"

    def test_task_subagent_only(self) -> None:
        """Test Task with only subagent type."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Task", "input": {"subagent_type": "Plan"}}
        assert analyzer._format_tool_description(block) == "Task (Plan)"

    def test_unknown_tool_fallback(self) -> None:
        """Test unknown tools fall back to 'Called <name>'."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "SomeUnknownTool", "input": {}}
        assert analyzer._format_tool_description(block) == "Called SomeUnknownTool"

    def test_missing_input_graceful(self) -> None:
        """Test graceful handling when input is missing."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Bash"}  # No input key
        result = analyzer._format_tool_description(block)
        assert result == "Ran: "  # Empty command

    def test_missing_name_graceful(self) -> None:
        """Test graceful handling when name is missing."""
        analyzer = TranscriptAnalyzer()
        block = {"input": {"command": "ls"}}  # No name key
        result = analyzer._format_tool_description(block)
        assert result == "Called unknown"

    def test_read_without_path(self) -> None:
        """Test Read tool without file_path falls back to generic message."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Read", "input": {}}  # No file_path
        result = analyzer._format_tool_description(block)
        assert result == "Called Read"

    def test_glob_without_pattern(self) -> None:
        """Test Glob tool without pattern falls back to generic message."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Glob", "input": {}}  # No pattern
        result = analyzer._format_tool_description(block)
        assert result == "Called Glob"

    def test_grep_without_pattern(self) -> None:
        """Test Grep tool without pattern falls back to generic message."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Grep", "input": {}}  # No pattern
        result = analyzer._format_tool_description(block)
        assert result == "Called Grep"

    def test_task_with_description_no_subagent(self) -> None:
        """Test Task with description but no subagent type."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Task", "input": {"description": "Find auth code"}}
        result = analyzer._format_tool_description(block)
        assert result == "Task: Find auth code"

    def test_task_without_description_or_subagent(self) -> None:
        """Test Task without description or subagent type."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Task", "input": {}}
        result = analyzer._format_tool_description(block)
        assert result == "Called Task"

    def test_edit_without_path(self) -> None:
        """Test Edit tool without file_path falls back to generic message."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Edit", "input": {}}  # No file_path
        result = analyzer._format_tool_description(block)
        assert result == "Called Edit"

    def test_write_without_path(self) -> None:
        """Test Write tool without file_path falls back to generic message."""
        analyzer = TranscriptAnalyzer()
        block = {"name": "Write", "input": {}}  # No file_path
        result = analyzer._format_tool_description(block)
        assert result == "Called Write"


class TestTaskProgress:
    """Tests for task_progress extraction."""

    def test_extract_task_progress_multiple_tasks(self) -> None:
        """Test task_progress captures multiple task interactions in chronological order."""
        turns = [
            {"type": "user", "message": {"content": "work on tasks"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "create_task",
                                "arguments": {"task_id": "gt-001", "title": "First task"},
                            },
                        },
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "claim_task",
                                "arguments": {"task_id": "gt-001"},
                            },
                        },
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "close_task",
                                "arguments": {"task_id": "gt-001"},
                            },
                        },
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "create_task",
                                "arguments": {"task_id": "gt-002", "title": "Second task"},
                            },
                        },
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        assert len(ctx.task_progress) == 4
        # Verify chronological order (not reversed)
        assert ctx.task_progress[0]["id"] == "gt-001"
        assert ctx.task_progress[0]["action"] == "create_task"
        assert ctx.task_progress[0]["title"] == "First task"
        assert ctx.task_progress[1]["action"] == "claim_task"
        assert ctx.task_progress[1]["title"] == "Task gt-001"  # No title in args
        assert ctx.task_progress[2]["action"] == "close_task"
        assert ctx.task_progress[3]["id"] == "gt-002"
        assert ctx.task_progress[3]["title"] == "Second task"

    def test_extract_task_progress_empty(self) -> None:
        """Test task_progress is empty when no gobby-tasks calls."""
        turns = [
            {"type": "user", "message": {"content": "do something"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Write", "input": {"file_path": "/a.py"}},
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        assert ctx.task_progress == []

    def test_extract_task_progress_reverse_order(self) -> None:
        """Test that task_progress is in chronological order despite reverse iteration."""
        turns = [
            {"type": "user", "message": {"content": "work"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "create_task",
                                "arguments": {"task_id": "gt-A", "title": "Alpha"},
                            },
                        },
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "update_task",
                                "arguments": {"task_id": "gt-B", "title": "Beta"},
                            },
                        },
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "close_task",
                                "arguments": {"task_id": "gt-C", "title": "Charlie"},
                            },
                        },
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        # Must be chronological: A, B, C
        assert [p["id"] for p in ctx.task_progress] == ["gt-A", "gt-B", "gt-C"]


class TestHandoffContext:
    """Tests for HandoffContext dataclass."""

    def test_default_values(self) -> None:
        """Test HandoffContext has correct default values."""
        ctx = HandoffContext()
        assert ctx.active_gobby_task is None
        assert ctx.task_progress == []
        assert ctx.files_modified == []
        assert ctx.git_commits == []
        assert ctx.git_status == ""
        assert ctx.initial_goal == ""
        assert ctx.recent_activity == []
        assert ctx.key_decisions is None
        assert ctx.active_worktree is None

    def test_custom_values(self) -> None:
        """Test HandoffContext with custom values."""
        ctx = HandoffContext(
            active_gobby_task={"id": "gt-123", "title": "Test task"},
            files_modified=["/path/to/file.py"],
            git_commits=[{"command": "git commit -m 'test'"}],
            git_status="On branch main",
            initial_goal="Implement feature X",
            recent_activity=["Called Read", "Called Write"],
            key_decisions=["Decision 1"],
            active_worktree={"path": "/worktree/path"},
        )
        assert ctx.active_gobby_task["id"] == "gt-123"
        assert ctx.files_modified == ["/path/to/file.py"]
        assert len(ctx.git_commits) == 1
        assert ctx.git_status == "On branch main"
        assert ctx.initial_goal == "Implement feature X"
        assert len(ctx.recent_activity) == 2
        assert ctx.key_decisions == ["Decision 1"]
        assert ctx.active_worktree["path"] == "/worktree/path"


class TestTranscriptAnalyzerInit:
    """Tests for TranscriptAnalyzer initialization."""

    def test_default_parser(self) -> None:
        """Test that TranscriptAnalyzer uses ClaudeTranscriptParser by default."""
        analyzer = TranscriptAnalyzer()
        assert isinstance(analyzer.parser, ClaudeTranscriptParser)

    def test_custom_parser(self) -> None:
        """Test that TranscriptAnalyzer accepts a custom parser."""
        mock_parser = Mock()
        analyzer = TranscriptAnalyzer(parser=mock_parser)
        assert analyzer.parser is mock_parser


class TestAnalyzerEdgeCases:
    """Additional edge case tests for comprehensive coverage."""

    def test_mcp_call_tool_missing_server_name(self) -> None:
        """Test mcp_call_tool blocks with missing server_name."""
        turns = [
            {"type": "user", "message": {"content": "do something"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "tool_name": "some_tool",  # Missing server_name
                                "arguments": {},
                            },
                        },
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        # Should not crash, active_gobby_task should be None
        assert ctx.active_gobby_task is None

    def test_mcp_call_tool_missing_tool_name(self) -> None:
        """Test MCP format description with missing tool_name."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp_call_tool",
            "input": {"server_name": "gobby-tasks"},  # Missing tool_name
        }
        result = analyzer._format_tool_description(block)
        assert result == "Called gobby-tasks.unknown"

    def test_mcp_call_tool_missing_server_name_format(self) -> None:
        """Test MCP format description with missing server_name."""
        analyzer = TranscriptAnalyzer()
        block = {
            "name": "mcp_call_tool",
            "input": {"tool_name": "some_tool"},  # Missing server_name
        }
        result = analyzer._format_tool_description(block)
        assert result == "Called unknown.some_tool"

    def test_replace_tool_for_file_modification(self) -> None:
        """Test that Replace tool captures file modifications."""
        turns = [
            {"type": "user", "message": {"content": "refactor code"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Replace",
                            "input": {"file_path": "/replaced.py"},
                        },
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        assert "/replaced.py" in ctx.files_modified

    def test_turns_without_message_key(self) -> None:
        """Test handling of turns missing the message key."""
        turns = [
            {"type": "user"},  # No message key - becomes first user, with empty content
            {"type": "assistant", "message": {}},  # Empty message
            {"type": "user", "message": {"content": "later goal"}},
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        # First user message is captured even if it has no content
        # (The code breaks on first user message found)
        assert ctx.initial_goal == ""

    def test_first_user_message_with_content_captured(self) -> None:
        """Test that initial goal is extracted from first user message with content."""
        turns = [
            {"type": "assistant", "message": {"content": "Hello!"}},  # Not a user turn
            {"type": "user", "message": {"content": "My actual goal"}},
            {"type": "user", "message": {"content": "Follow-up question"}},
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        # Should get the first user message, not the second
        assert ctx.initial_goal == "My actual goal"

    def test_initial_goal_with_dict_content(self) -> None:
        """Test initial goal extraction when content is a dict."""
        turns = [
            {"type": "user", "message": {"content": {"key": "value"}}},
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        # str() of a dict
        assert "key" in ctx.initial_goal
        assert "value" in ctx.initial_goal

    def test_bash_without_git_commit(self) -> None:
        """Test Bash commands without git commit don't add to git_commits."""
        turns = [
            {"type": "user", "message": {"content": "run commands"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "npm install"}},
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        assert ctx.git_commits == []

    def test_gobby_tasks_with_empty_arguments(self) -> None:
        """Test gobby-tasks calls with missing arguments key."""
        turns = [
            {"type": "user", "message": {"content": "work on task"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "list_tasks",
                                # No arguments key at all
                            },
                        },
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        # Should handle gracefully
        assert ctx.active_gobby_task is None

    def test_multiple_tool_calls_in_single_turn(self) -> None:
        """Test extraction when multiple tool calls are in a single turn."""
        turns = [
            {"type": "user", "message": {"content": "do many things"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/b.py"}},
                        {"type": "tool_use", "name": "Write", "input": {"file_path": "/c.py"}},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "git commit -m 'changes'"},
                        },
                        {
                            "type": "tool_use",
                            "name": "mcp_call_tool",
                            "input": {
                                "server_name": "gobby-tasks",
                                "tool_name": "update_task",
                                "arguments": {"task_id": "gt-multi"},
                            },
                        },
                    ]
                },
            },
        ]

        analyzer = TranscriptAnalyzer()
        ctx = analyzer.extract_handoff_context(turns)

        # Read doesn't modify files
        assert "/a.py" not in ctx.files_modified
        # Edit and Write do
        assert "/b.py" in ctx.files_modified
        assert "/c.py" in ctx.files_modified
        # Git commit captured
        assert len(ctx.git_commits) == 1
        # Task captured
        assert ctx.active_gobby_task["id"] == "gt-multi"
        # Recent activity should have 5 items
        assert len(ctx.recent_activity) == 5


def test_extract_key_decisions() -> None:
    """Test that key decisions are extracted from assistant text blocks."""
    turns = [
        {"type": "user", "message": {"content": "help me"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "I decided to use a factory pattern because it provides better extensibility.",
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "I opted for SQLite instead of PostgreSQL for local storage.",
                    },
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert ctx.key_decisions is not None
    assert len(ctx.key_decisions) == 2
    assert "factory pattern" in ctx.key_decisions[0]
    assert "SQLite" in ctx.key_decisions[1]


def test_extract_key_decisions_caps_at_10() -> None:
    """Test that key decisions are capped at 10."""
    turns = [{"type": "user", "message": {"content": "help me"}}]
    for i in range(15):
        turns.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"I decided to use approach {i} because it works well.",
                        },
                    ]
                },
            }
        )

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert ctx.key_decisions is not None
    assert len(ctx.key_decisions) == 10
    # Verify the retained decisions are the first 10 extracted
    for i, decision in enumerate(ctx.key_decisions):
        assert f"approach {i}" in decision


def test_initial_goal_handles_list_content() -> None:
    """Test that initial goal handles nested content block lists."""
    turns = [
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "Please fix"},
                    {"type": "text", "text": "the login bug"},
                ]
            },
        },
    ]

    analyzer = TranscriptAnalyzer()
    ctx = analyzer.extract_handoff_context(turns)

    assert ctx.initial_goal == "Please fix the login bug"
