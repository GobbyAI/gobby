from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any, cast

import pytest

from gobby.hooks._normalization_shell import canonicalize_shell_tool_name, is_shell_tool
from gobby.memory.digest import _extract_digest_pairs
from gobby.sessions.transcripts.base import TokenUsage, TranscriptParser, raw_lines_from_texts
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.codex_items import (
    mcp_item_failure,
    normalize_command_execution,
)
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser
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
    failures = [
        ({"result": {"success": False, "error": "dict"}}, "dict"),
        ({"result": {"Ok": '{"success":false,"error":"ok-json"}'}}, "ok-json"),
        (
            {"result": {"structuredContent": {"success": False, "error": "structured"}}},
            "structured",
        ),
        ({"result": {"Err": "transport"}}, "transport"),
    ]
    for item, expected in failures:
        assert mcp_item_failure(item) == expected
    assert mcp_item_failure({"status": "completed", "result": {"success": True}}) is None
    assert mcp_item_failure({"status": "completed"}) is None


def test_grok_terminal_alias_is_ledger_local() -> None:
    name, tool_input = canonical_tool_name("run_terminal_command", {"command": "git status"})
    droid_name, droid_input = canonical_tool_name("Execute", {"command": "uv run pytest"})

    assert (name, tool_input) == ("Bash", {"command": "git status"})
    assert (droid_name, droid_input) == ("Bash", {"command": "uv run pytest"})
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

    for dispatcher in (
        "mcp__gobby__call_tool",
        "gobby___call_tool",
        "provider___call_tool",
    ):
        for wrapper in wrappers:
            assert canonical_tool_name(dispatcher, wrapper) == (
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

    entries = [
        ToolActivityEntry(
            "Bash",
            {"command": f"printf '{index}\n\t\x1b' {'x' * 120}"},
            resolved=True,
        )
        for index in range(120)
    ]
    rendered = render_tool_activity(entries)

    assert len(rendered.splitlines()) <= 80
    assert len(rendered) <= 6000
    assert "\t" not in rendered
    assert "\x1b" not in rendered
    assert _represented_call_count(rendered) == len(entries)


@pytest.mark.parametrize(
    ("parser_factory", "turns", "expected_lines"),
    [
        pytest.param(
            ClaudeTranscriptParser,
            [
                {"message": {"role": "user", "content": "inspect"}},
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "success",
                                "name": "Read",
                                "input": {"path": "success.txt"},
                            },
                            {
                                "type": "tool_use",
                                "id": "failed",
                                "name": "Read",
                                "input": {"path": "failed.txt"},
                            },
                            {
                                "type": "tool_use",
                                "id": "pending",
                                "name": "Read",
                                "input": {"path": "pending.txt"},
                            },
                        ],
                    }
                },
                {
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "success", "content": "ok"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "failed",
                                "content": "permission denied",
                                "is_error": True,
                            },
                        ],
                    }
                },
                {"message": {"role": "assistant", "content": "done"}},
            ],
            [
                "- Read success.txt",
                "- Read failed.txt ! failed: permission denied",
                "- Read pending.txt (no result recorded)",
            ],
            id="claude",
        ),
        pytest.param(
            CodexTranscriptParser,
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "inspect"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "success",
                        "name": "functions.exec",
                        "input": 'const r = await tools.exec_command({cmd:"uv run pytest"}); text(r);',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "success",
                        "output": [
                            {
                                "type": "input_text",
                                "text": json.dumps({"exit_code": 0, "output": "passed"}),
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "failed",
                        "name": "functions.exec",
                        "input": (
                            'const r = await tools.exec_command({cmd:"uv run pytest -k broken"}); '
                            "text(r);"
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "failed",
                        "output": [
                            {
                                "type": "input_text",
                                "text": json.dumps({"exit_code": 1, "output": "one failed"}),
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "pending-direct",
                        "name": "functions.exec_command",
                        "arguments": json.dumps({"cmd": "tail -f /tmp/widget.log"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "unknown",
                        "name": "functions.exec",
                        "input": 'const r = await tools.exec_command({cmd:"ambiguous"}); text(r);',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "unknown",
                        "output": [
                            {
                                "type": "input_text",
                                "text": json.dumps(
                                    {"exit_code": 0, "exitCode": 1, "output": "conflict"}
                                ),
                            }
                        ],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
            ],
            [
                "- Bash uv run pytest",
                "- Bash uv run pytest -k broken ! failed: one failed",
                "- Bash tail -f /tmp/widget.log (no result recorded)",
                "- Bash ambiguous (no result recorded)",
            ],
            id="codex",
        ),
        pytest.param(
            GrokTranscriptParser,
            [
                {
                    "params": {
                        "update": {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": "inspect"},
                        }
                    }
                },
                *[
                    {"params": {"update": update}}
                    for update in (
                        {
                            "sessionUpdate": "tool_call",
                            "title": "run_terminal_command",
                            "toolCallId": "success",
                            "rawInput": {"command": "uv run pytest"},
                        },
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "success",
                            "status": "completed",
                            "content": {"type": "text", "text": "passed"},
                        },
                        {
                            "sessionUpdate": "tool_call",
                            "title": "run_terminal_command",
                            "toolCallId": "failed",
                            "rawInput": {"command": "uv run pytest -k broken"},
                        },
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "failed",
                            "status": "failed",
                            "content": {"type": "text", "text": "one failed"},
                        },
                        {
                            "sessionUpdate": "tool_call",
                            "title": "read_file",
                            "toolCallId": "pending",
                            "rawInput": {"path": "pending.txt"},
                        },
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "done"},
                        },
                    )
                ],
            ],
            [
                "- Bash uv run pytest",
                "- Bash uv run pytest -k broken ! failed: one failed",
                "- Read pending.txt (no result recorded)",
            ],
            id="grok",
        ),
        pytest.param(
            QwenTranscriptParser,
            [
                {"type": "user", "message": {"parts": [{"text": "inspect"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": outcome,
                                    "name": "Read",
                                    "args": {"path": f"{outcome}.txt"},
                                }
                            }
                            for outcome in ("success", "failed", "pending")
                        ]
                    },
                },
                {
                    "type": "tool_result",
                    "toolCallResult": {"callId": "success", "status": "completed"},
                    "message": {
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": "success",
                                    "name": "Read",
                                    "response": {"output": "ok"},
                                }
                            }
                        ]
                    },
                },
                {
                    "type": "tool_result",
                    "toolCallResult": {"callId": "failed", "status": "error"},
                    "message": {
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": "failed",
                                    "name": "Read",
                                    "response": {"output": "permission denied"},
                                }
                            }
                        ]
                    },
                },
                {"type": "assistant", "message": {"parts": [{"text": "done"}]}},
            ],
            [
                "- Read success.txt",
                "- Read failed.txt ! failed: permission denied",
                "- Read pending.txt (no result recorded)",
            ],
            id="qwen",
        ),
        pytest.param(
            DroidTranscriptParser,
            [
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "inspect"}],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "success",
                                "name": "Execute",
                                "input": {"command": "uv run pytest"},
                            },
                            {
                                "type": "tool_use",
                                "id": "failed",
                                "name": "Execute",
                                "input": {"command": "uv run pytest -k broken"},
                            },
                            {
                                "type": "tool_use",
                                "id": "pending",
                                "name": "gobby___call_tool",
                                "input": {
                                    "server_name": "gobby-tasks",
                                    "tool_name": "claim_task",
                                    "arguments": {"task_id": "#20728"},
                                },
                            },
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "success",
                                "content": {"type": "text", "text": "passed"},
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "failed",
                                "content": {"type": "text", "text": "one failed"},
                                "is_error": True,
                            },
                        ],
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    },
                },
            ],
            [
                "- Bash uv run pytest",
                "- Bash uv run pytest -k broken ! failed: one failed",
                "- mcp gobby-tasks:claim_task task_id=#20728 (no result recorded)",
            ],
            id="droid",
        ),
    ],
)
def test_ledger_distinguishes_success_failure_and_missing_result(
    parser_factory: Callable[[], TranscriptParser],
    turns: list[dict[str, Any]],
    expected_lines: list[str],
) -> None:
    messages = parser_factory().extract_last_messages(
        turns,
        num_pairs=len(turns),
        include_tool_activity=True,
    )
    ledger = next(message["tool_activity"] for message in messages if "tool_activity" in message)

    assert ledger.splitlines() == [ACTIVITY_HEADER, *expected_lines]


def test_render_tool_activity_truncation_keeps_evidence() -> None:
    entries = [ToolActivityEntry("Read", {"path": "/tmp/repeated"}, resolved=True)] * 7
    entries.extend(
        ToolActivityEntry("Read", {"file_path": f"/tmp/{index}.txt"}, resolved=True)
        for index in range(105)
    )
    entries.extend(
        [
            ToolActivityEntry("search_replace", {"file_path": "/repo/new.py"}, resolved=True),
            ToolActivityEntry("MultiEdit", {"file_path": "/repo/multi.py"}, resolved=True),
            ToolActivityEntry(
                "NotebookEdit",
                {"notebook_path": "/repo/analysis.ipynb"},
                resolved=True,
            ),
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
        ToolActivityEntry("Read", {"path": f"/tmp/after-{index}"}, resolved=True)
        for index in range(20)
    )
    entries.extend(
        ToolActivityEntry("Read", {"file_path": "/tmp/tail.txt"}, resolved=True) for _ in range(11)
    )

    rendered = render_tool_activity(entries)

    assert len(rendered.splitlines()) <= 80
    assert len(rendered) <= 6000
    assert "- search_replace /repo/new.py" in rendered
    assert "- MultiEdit /repo/multi.py" in rendered
    assert "- NotebookEdit /repo/analysis.ipynb" in rendered
    assert "- Bash git commit -m ledger" in rendered
    assert "- mcp gobby-tasks:close_task task_id=#20728 commit_sha=abc1234" in rendered
    assert "- Bash false ! failed: exit 1" in rendered
    assert "/tmp/tail.txt (x11)" in rendered
    assert "more tool calls omitted" in rendered
    assert _represented_call_count(rendered) == len(entries)


def _represented_call_count(rendered: str) -> int:
    represented = 0
    omitted = 0
    for line in rendered.splitlines()[1:]:
        omission = re.fullmatch(r"- … (\d+) more tool calls omitted", line)
        if omission is not None:
            omitted = int(omission.group(1))
            continue
        collapsed = re.search(r" \(x(\d+)\)$", line)
        represented += int(collapsed.group(1)) if collapsed is not None else 1
    return represented + omitted


def test_observational_scans_leave_parser_state_untouched() -> None:
    timestamp = "2026-08-26T12:00:00Z"

    qwen = QwenTranscriptParser(session_id="qwen-session")
    qwen_control = QwenTranscriptParser(session_id="qwen-session")
    qwen._last_tool_use_id = qwen_control._last_tool_use_id = "seed"
    qwen_turns = [
        {"type": "user", "timestamp": timestamp, "message": {"parts": [{"text": "inspect"}]}},
        {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "parts": [{"functionCall": {"id": "call-1", "name": "Read", "args": {"path": "x"}}}]
            },
        },
    ]
    qwen_before = _private_state(qwen)
    qwen_messages = qwen.extract_last_messages(qwen_turns, include_tool_activity=True)
    qwen_fresh_messages = QwenTranscriptParser(session_id="qwen-session").extract_last_messages(
        qwen_turns,
        include_tool_activity=True,
    )
    qwen_following = [
        {
            "type": "tool_result",
            "timestamp": timestamp,
            "toolCallResult": {"status": "completed"},
            "message": {
                "parts": [
                    {
                        "functionResponse": {
                            "name": "Read",
                            "response": {"output": "ok"},
                        }
                    }
                ]
            },
        }
    ]

    assert _private_state(qwen) == qwen_before == _private_state(qwen_control)
    assert qwen.snapshot_state() == qwen_control.snapshot_state()
    assert qwen_messages == qwen_fresh_messages
    assert _parse_events(qwen, qwen_following) == _parse_events(qwen_control, qwen_following)

    codex = CodexTranscriptParser(session_id="codex-session")
    codex_control = CodexTranscriptParser(session_id="codex-session")
    codex_seed = [
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "custom_tool_call",
                "call_id": "seed-exec",
                "name": "functions.exec",
                "input": 'const r = await tools.exec_command({cmd:"seed"}); text(r);',
            },
        },
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "tool_search_call",
                "call_id": "seed-search",
                "arguments": {"query": "seed"},
            },
        },
    ]
    assert _parse_events(codex, codex_seed) == _parse_events(codex_control, codex_seed)
    codex_before = _private_state(codex)
    codex_turns = [
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "inspect"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "function_call",
                "call_id": "scan-call",
                "name": "Read",
                "arguments": json.dumps({"path": "widget.py"}),
            },
        },
    ]
    codex_messages = codex.extract_last_messages(codex_turns, include_tool_activity=True)
    codex_fresh_messages = CodexTranscriptParser(session_id="codex-session").extract_last_messages(
        codex_turns,
        include_tool_activity=True,
    )
    codex_following = [
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "seed-exec",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps({"exit_code": 0, "output": "done"}),
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "tool_search_output",
                "call_id": "seed-search",
                "tools": [],
            },
        },
    ]

    assert _private_state(codex) == codex_before == _private_state(codex_control)
    assert codex.snapshot_state() == codex_control.snapshot_state()
    assert codex_messages == codex_fresh_messages
    assert _parse_events(codex, codex_following) == _parse_events(codex_control, codex_following)

    droid = DroidTranscriptParser(session_id="droid-session")
    droid_control = DroidTranscriptParser(session_id="droid-session")
    for parser in (droid, droid_control):
        parser._last_assistant_index = 7
        parser._sidecar_usage = TokenUsage(input_tokens=10, output_tokens=4)
        parser._sidecar_model = "droid-model"
        parser._last_emitted_usage = TokenUsage(input_tokens=8, output_tokens=3)
    droid_before = _private_state(droid)
    droid_turns = [
        {
            "type": "message",
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "inspect"}],
            },
        },
        {
            "type": "message",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "scan-call",
                        "name": "Read",
                        "input": {"path": "widget.py"},
                    }
                ],
            },
        },
    ]
    droid_messages = droid.extract_last_messages(droid_turns, include_tool_activity=True)
    droid_fresh_messages = DroidTranscriptParser(session_id="droid-session").extract_last_messages(
        droid_turns,
        include_tool_activity=True,
    )
    droid_following = [
        {
            "type": "message",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "continued"}],
            },
        }
    ]

    assert _private_state(droid) == droid_before == _private_state(droid_control)
    assert droid.snapshot_state() == droid_control.snapshot_state()
    assert droid_messages == droid_fresh_messages
    assert _parse_events(droid, droid_following) == _parse_events(droid_control, droid_following)

    digest_turns = [
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "message",
                "role": role,
                "content": [
                    {
                        "type": "input_text" if role == "user" else "output_text",
                        "text": text,
                    }
                ],
            },
        }
        for role, text in (("user", "one"), ("assistant", "done"), ("user", "two"))
    ]
    reused = CodexTranscriptParser(session_id="digest")
    reused_counts = (
        len(_extract_digest_pairs(reused, digest_turns)),
        len(_extract_digest_pairs(reused, digest_turns[:2])),
    )
    fresh_counts = (
        len(_extract_digest_pairs(CodexTranscriptParser(session_id="digest"), digest_turns)),
        len(_extract_digest_pairs(CodexTranscriptParser(session_id="digest"), digest_turns[:2])),
    )

    assert reused_counts == fresh_counts


def _private_state(parser: TranscriptParser) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for name, value in vars(parser).items():
        if not name.startswith("_"):
            continue
        snapshot = getattr(value, "snapshot_state", None)
        state[name] = snapshot() if callable(snapshot) else copy.deepcopy(value)
    return state


def _parse_events(
    parser: TranscriptParser,
    turns: list[dict[str, Any]],
) -> list[Any]:
    texts = [json.dumps(turn) for turn in turns]
    return list(parser.iter_parse_events(raw_lines_from_texts(texts)))
