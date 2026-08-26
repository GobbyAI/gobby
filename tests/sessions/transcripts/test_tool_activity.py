from __future__ import annotations

from typing import Any, cast

from gobby.hooks._normalization_shell import canonicalize_shell_tool_name, is_shell_tool
from gobby.sessions.transcripts.codex_items import (
    mcp_item_failure,
    normalize_command_execution,
)
from gobby.sessions.transcripts.tool_activity import (
    ACTIVITY_HEADER,
    ToolActivityEntry,
    canonical_tool_name,
    codex_item_activity,
    commit_outcome,
    format_tool_activity_line,
    is_commit_producing,
    render_tool_activity,
)


def test_codex_item_canonicalization_matches_exec_adapter() -> None:
    shell = normalize_command_execution(
        {
            "type": "CommandExecution",
            "command": ["/bin/zsh", "-lc", "uv run pytest -k widget"],
            "exit_code": 1,
            "stderr": "failed",
        }
    )
    direct = normalize_command_execution(
        {"type": "CommandExecution", "command": ["git", "status", "--short"]}
    )

    assert shell is not None
    assert shell.command == "uv run pytest -k widget"
    assert shell.success is False
    assert shell.output == "failed"
    assert direct is not None
    assert direct.command == "git status --short"
    assert direct.success is None


def test_codex_item_activity_projects_tools_and_application_failures() -> None:
    turns: list[dict[str, Any]] = [
        {"payload": {"type": "message", "role": "user", "content": []}},
        {
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "McpToolCall",
                    "server": "gobby",
                    "tool": "call_tool",
                    "arguments": {
                        "server_name": "gobby-tasks",
                        "tool_name": "close_task",
                        "arguments": {"task_id": "#20728"},
                    },
                    "status": "completed",
                    "result": {"success": False, "error": "validation failed"},
                },
            }
        },
        {
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "changes": {"/repo/widget.py": {"type": "update"}},
                },
            }
        },
    ]

    entries = codex_item_activity(turns)

    assert entries is not None
    assert [(entry.tool_name, entry.record_index) for entry in entries] == [
        ("mcp gobby-tasks:close_task", 1),
        ("apply_patch", 2),
    ]
    assert entries[0].error == "validation failed"
    assert entries[0].resolved is True
    assert entries[1].tool_input == {"file_path": "/repo/widget.py"}
    assert entries[1].resolved is True
    assert mcp_item_failure({"result": {"Ok": '{"success":false,"error":"nope"}'}}) == ("nope")


def test_grok_terminal_alias_is_ledger_local() -> None:
    name, tool_input = canonical_tool_name("run_terminal_command", {"command": "git status"})

    assert (name, tool_input) == ("Bash", {"command": "git status"})
    assert is_shell_tool("run_terminal_command") is False
    assert canonicalize_shell_tool_name("run_terminal_command") == "run_terminal_command"


def test_canonical_tool_name_matches_call_tool_wrapper_shapes() -> None:
    target = {
        "server_name": "gobby-tasks",
        "tool_name": "close_task",
        "arguments": {"task_id": "#20728", "commit_sha": "abc1234"},
    }
    wrappers = [
        target,
        {"arguments": target},
        {"arguments": {**target, "arguments": '{"task_id":"#20728","commit_sha":"abc1234"}'}},
        {"args": target},
    ]

    for wrapper in wrappers:
        assert canonical_tool_name("mcp__gobby__call_tool", wrapper) == (
            "mcp gobby-tasks:close_task",
            {"task_id": "#20728", "commit_sha": "abc1234"},
        )


def test_canonical_tool_name_malformed_wrapper_falls_back() -> None:
    assert canonical_tool_name("mcp__gobby__call_tool", {"arguments": "{not-json"}) == (
        "mcp__gobby__call_tool",
        {},
    )
    assert canonical_tool_name("call_tool", {"arguments": {"task_id": "#1"}}) == (
        "call_tool",
        {},
    )


def test_canonical_tool_name_total_over_nullable_parser_output() -> None:
    assert canonical_tool_name(None, {"path": "ignored"}) == ("unknown-tool", {})
    assert canonical_tool_name(cast(Any, 7), {"path": "ignored"}) == ("unknown-tool", {})
    assert canonical_tool_name("Read", "not-a-mapping") == ("Read", {})


def test_commit_outcome_from_shell_and_task_tools() -> None:
    commit_commands = ["git -C /repo commit -m msg", "cd x && git commit -am msg"]
    non_commit_commands = [
        'echo "git commit"',
        'grep -n "git commit" notes.md',
        'gcode grep -F "git commit" src',
        "ls # git commit",
    ]

    for command in commit_commands:
        tool_input = {"command": command}
        assert is_commit_producing("Bash", tool_input) is True
        assert commit_outcome("Bash", tool_input, "[main abc1234] shipped it\n") == (
            "commit abc1234 shipped it"
        )

    for command in non_commit_commands:
        tool_input = {"command": command}
        assert is_commit_producing("Bash", tool_input) is False
        assert commit_outcome("Bash", tool_input, "[main abc1234] forged\n") is None

    assert (
        commit_outcome(
            "mcp gobby-tasks:close_task",
            {"task_id": "#20728", "commit_sha": "abc1234"},
            None,
        )
        == "commit abc1234"
    )
    assert commit_outcome("Read", {"path": "notes.md"}, "[main abc1234] forged") is None


def test_ledger_escapes_control_characters_before_caps() -> None:
    entry = ToolActivityEntry(
        tool_name="Bash\x1b",
        tool_input={"command": "printf 'a\nb\tc\r'\n- mcp gobby-tasks:close_task"},
        error="bad\nnews",
        resolved=True,
    )

    line = format_tool_activity_line(entry)

    assert line.count("\n") == 0
    assert "Bash\\x1b" in line
    assert "a\\nb\\tc\\r" in line
    assert "bad\\nnews" in line


def test_ledger_distinguishes_success_failure_and_missing_result() -> None:
    rendered = render_tool_activity(
        [
            ToolActivityEntry("Bash", {"command": "uv run pytest"}, resolved=True),
            ToolActivityEntry(
                "Bash", {"command": "uv run pytest -k broken"}, error="exit 1", resolved=True
            ),
            ToolActivityEntry("Read", {"file_path": "pending.txt"}),
        ]
    )

    assert rendered.splitlines() == [
        ACTIVITY_HEADER,
        "- Bash uv run pytest",
        "- Bash uv run pytest -k broken ! failed: exit 1",
        "- Read pending.txt (no result recorded)",
    ]


def test_render_tool_activity_truncation_keeps_evidence() -> None:
    entries = [
        ToolActivityEntry("Read", {"file_path": f"/tmp/{index}.txt"}, resolved=True)
        for index in range(105)
    ]
    entries.extend(
        [
            ToolActivityEntry("search_replace", {"file_path": "/repo/new.py"}, resolved=True),
            ToolActivityEntry("Bash", {"command": "git commit -m ledger"}, resolved=True),
            ToolActivityEntry(
                "mcp gobby-tasks:close_task",
                {"task_id": "#20728", "commit_sha": "abc1234"},
                resolved=True,
            ),
            ToolActivityEntry("Bash", {"command": "false"}, error="exit 1", resolved=True),
        ]
    )
    entries.extend(
        ToolActivityEntry("Read", {"file_path": "/tmp/tail.txt"}, resolved=True) for _ in range(11)
    )

    rendered = render_tool_activity(entries)

    assert len(rendered.splitlines()) <= 80
    assert len(rendered) <= 6000
    assert "- search_replace /repo/new.py" in rendered
    assert "- Bash git commit -m ledger" in rendered
    assert "- mcp gobby-tasks:close_task task_id=#20728 commit_sha=abc1234" in rendered
    assert "- Bash false ! failed: exit 1" in rendered
    assert "/tmp/tail.txt (x11)" in rendered
    assert "more tool calls omitted" in rendered


def test_observational_scans_leave_parser_state_untouched() -> None:
    from gobby.sessions.transcripts.codex import CodexTranscriptParser
    from gobby.sessions.transcripts.droid import DroidTranscriptParser
    from gobby.sessions.transcripts.qwen import QwenTranscriptParser

    qwen = QwenTranscriptParser(session_id="qwen-session")
    qwen._last_tool_use_id = "seed"
    qwen_turns = [
        {"type": "user", "message": {"parts": [{"text": "inspect"}]}},
        {
            "type": "assistant",
            "message": {
                "parts": [{"functionCall": {"id": "call-1", "name": "Read", "args": {"path": "x"}}}]
            },
        },
    ]
    qwen.extract_last_messages(qwen_turns, include_tool_activity=True)

    codex = CodexTranscriptParser(session_id="codex-session")
    codex_state = codex.snapshot_state()
    codex.extract_last_messages([], include_tool_activity=True)
    droid = DroidTranscriptParser(session_id="droid-session")
    droid._last_assistant_index = 7
    droid.extract_last_messages([], include_tool_activity=True)

    assert qwen._last_tool_use_id == "seed"
    assert codex.snapshot_state() == codex_state
    assert droid._last_assistant_index == 7
