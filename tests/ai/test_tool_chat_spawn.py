"""Tests for the Family B spawn tool_chat adapters (gcode-direct).

Covers the shared gcode-direct prompt, stream parsers (codex/droid/qwen),
and all four adapter classes' command construction + result mapping with the
external spawn stubbed out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from gobby.ai import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.ai import _tool_chat_spawn as spawn
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolPolicy
from gobby.ai._tool_chat_spawn import (
    ACPSpawnToolChatAdapter,
    CodexSpawnToolChatAdapter,
    DroidSpawnToolChatAdapter,
    GrokSpawnToolChatAdapter,
    QwenSpawnToolChatAdapter,
    compose_gcode_direct_prompt,
    parse_codex_stream,
    parse_droid_stream,
    parse_grok_session_signals,
    parse_qwen_stream,
)
from gobby.ai._tool_chat_tools import ToolPolicyError


def _binding(
    provider: str = "codex", style: AIAdapterStyle = AIAdapterStyle.DAEMON
) -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider=provider,
        adapter_style=style,
        available=True,
        models=("gpt-5.5",),
        metadata={},
    )


def _request(**overrides: Any) -> ToolChatRequest:
    base: dict[str, Any] = {
        "prompt": "Document the auth module.",
        "tool_policy": ToolPolicy(cli="gcode", tools=("search", "outline")),
        "project_path": "/repo",
    }
    base.update(overrides)
    return ToolChatRequest(**base)


# --- Shared prompt ---------------------------------------------------------


def test_compose_gcode_direct_prompt_includes_tools_and_project() -> None:
    request = _request(system_prompt="You are a code historian.")

    prompt = compose_gcode_direct_prompt(request)

    assert "You are a code historian." in prompt
    assert "Document the auth module." in prompt
    assert "search, outline" in prompt
    assert "--project /repo" in prompt
    assert "gcode" in prompt
    assert "file:line" in prompt
    # Must NOT mention gobby-index or MCP
    assert "gobby-index" not in prompt
    assert "call_tool" not in prompt


# --- Codex stream parser ---------------------------------------------------


def test_parse_codex_stream_extracts_narrative_and_tool_counts() -> None:
    stream = "\n".join(
        [
            "Reading prompt from stdin...",
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"item.completed","item":{"id":"i0","type":"command_execution",'
            '"command":"/bin/zsh -lc gcode search","exit_code":0}}',
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message",'
            '"text":"## Auth\\n\\nNarrative citing src/auth.rs:10."}}',
            '{"type":"turn.completed","usage":{}}',
        ]
    )

    text, total, breakdown = parse_codex_stream(stream)

    assert text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert total == 1
    assert breakdown == {"command_execution": 1}


def test_parse_codex_stream_skips_non_json_lines() -> None:
    text, total, _ = parse_codex_stream("not json\n{}\n")

    assert text == ""
    assert total == 0


def test_parse_codex_stream_counts_multiple_tool_calls() -> None:
    stream = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"command_execution"}}',
            '{"type":"item.completed","item":{"type":"command_execution"}}',
        ]
    )

    _, total, breakdown = parse_codex_stream(stream)

    assert total == 2
    assert breakdown == {"command_execution": 2}


# --- Droid stream parser ---------------------------------------------------


# Real droid `--output-format stream-json` records (captured from droid 0.159.1).
_DROID_STREAM = "\n".join(
    [
        '{"type":"system","subtype":"init","tools":["Read"],"model":"m"}',
        '{"type":"message","role":"user","text":"Document auth."}',
        '{"type":"tool_call","toolId":"Execute","toolName":"Execute",'
        '"parameters":{"command":"gcode search --project /repo auth"}}',
        '{"type":"tool_result","toolId":"Execute","isError":false,"value":"hit"}',
        '{"type":"tool_call","toolName":"Execute"}',
        '{"type":"completion","finalText":"## Auth\\n\\nNarrative citing src/auth.rs:10.",'
        '"numTurns":3}',
    ]
)


def test_parse_droid_stream_extracts_final_text_and_tool_counts() -> None:
    text, total, breakdown = parse_droid_stream(_DROID_STREAM)

    assert text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert total == 2
    assert breakdown == {"Execute": 2}


def test_parse_droid_stream_falls_back_to_last_assistant_message() -> None:
    stream = "\n".join(
        [
            '{"type":"message","role":"assistant","text":"first"}',
            '{"type":"message","role":"assistant","text":"## Final answer"}',
        ]
    )

    text, total, _ = parse_droid_stream(stream)

    assert text == "## Final answer"
    assert total == 0


# --- Qwen stream parser ----------------------------------------------------


def test_parse_qwen_stream_extracts_narrative_and_tool_counts() -> None:
    # Real qwen-code stream shape: assistant/user messages nest content under
    # `message` (Claude-Code stream format), captured from qwen-code 0.x.
    stream = "\n".join(
        [
            '{"type":"system","tools":[],"model":"m"}',
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"thinking","thinking":"..."},'
            '{"type":"tool_use","name":"run_shell_command","input":{}}]}}',
            '{"type":"user","message":{"role":"user","content":'
            '[{"type":"tool_result","tool_use_id":"1","is_error":false}]}}',
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"tool_use","name":"run_shell_command"}]}}',
            '{"type":"result","result":"## Auth\\n\\nNarrative citing src/auth.rs:10.",'
            '"usage":{"input_tokens":100,"output_tokens":50}}',
        ]
    )

    text, total, breakdown = parse_qwen_stream(stream)

    assert text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert total == 2
    assert breakdown == {"run_shell_command": 2}


def test_parse_qwen_stream_skips_non_json_lines() -> None:
    text, total, _ = parse_qwen_stream("warning text\n{}\n")

    assert text == ""
    assert total == 0


# --- Grok session signals parser -------------------------------------------


def test_parse_grok_session_signals_extracts_tool_counts_from_updates(
    tmp_path: Path,
) -> None:
    """updates.jsonl is the primary source: counts tool_call events by title."""
    updates = "\n".join(
        [
            json.dumps(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "c1",
                            "title": "run_terminal_command",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "c1",
                            "status": "completed",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "c2",
                            "title": "read_file",
                        },
                    },
                }
            ),
        ]
    )
    (tmp_path / "updates.jsonl").write_text(updates, encoding="utf-8")

    total, breakdown = parse_grok_session_signals(tmp_path)

    assert total == 2
    assert breakdown == {"run_terminal_command": 1, "read_file": 1}


def test_parse_grok_session_signals_falls_back_to_signals_json(
    tmp_path: Path,
) -> None:
    """When updates.jsonl has no tool_call events, signals.json provides the total."""
    (tmp_path / "updates.jsonl").write_text(
        json.dumps({"method": "session/update", "params": {"update": {"sessionUpdate": "text"}}}),
        encoding="utf-8",
    )
    (tmp_path / "signals.json").write_text(
        json.dumps({"toolCallCount": 3, "toolsUsed": ["run_terminal_command", "read_file"]}),
        encoding="utf-8",
    )

    total, breakdown = parse_grok_session_signals(tmp_path)

    assert total == 3
    assert "run_terminal_command" in breakdown
    assert "read_file" in breakdown


def test_parse_grok_session_signals_returns_zeros_when_files_missing(
    tmp_path: Path,
) -> None:
    total, breakdown = parse_grok_session_signals(tmp_path)

    assert total == 0
    assert breakdown == {}


def test_resolve_grok_session_dir_finds_by_encoded_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_resolve_grok_session_dir locates the session dir via URL-encoded cwd."""
    fake_home = tmp_path / "fake-home"
    sessions_root = fake_home / ".grok" / "sessions"
    work_dir = Path("/var/folders/test/tool-chat-grok-abc")
    encoded = quote(str(work_dir), safe="")
    session_dir = sessions_root / encoded / "session-123"
    session_dir.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    from gobby.ai._tool_chat_spawn import _resolve_grok_session_dir

    result = _resolve_grok_session_dir("session-123", work_dir)

    assert result == session_dir


def test_resolve_grok_session_dir_returns_none_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from gobby.ai._tool_chat_spawn import _resolve_grok_session_dir

    result = _resolve_grok_session_dir("nonexistent", Path("/nonexistent/cwd"))

    assert result is None


# --- Codex adapter ---------------------------------------------------------


def test_codex_build_command_uses_json_sandbox_and_gcode_prompt() -> None:
    adapter = CodexSpawnToolChatAdapter(command_path="codex")
    request = _request(reasoning_effort="high")
    output_path = Path("/tmp/last-message.txt")

    command = adapter._build_command(request, model="gpt-5.5", output_path=output_path)

    assert command[0] == "codex"
    assert "exec" in command
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in command
    assert "--json" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--output-last-message") + 1] == str(output_path)
    assert command[command.index("--model") + 1] == "gpt-5.5"
    # The composed prompt is the final positional argument.
    assert "Document the auth module." in command[-1]
    assert "gcode" in command[-1]


async def test_codex_adapter_captures_narrative_and_counts_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_jsonl = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"command_execution"}}',
            '{"type":"item.completed","item":{"type":"command_execution"}}',
        ]
    )

    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        (neutral_cwd / "last-message.txt").write_text(
            "## Auth\n\nGrounded narrative citing src/auth.rs:10.",
            encoding="utf-8",
        )
        # gcode must be on PATH (~/.gobby/bin prepended)
        assert ".gobby/bin" in env_overrides["PATH"]
        return codex_jsonl

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = CodexSpawnToolChatAdapter(command_path="codex")

    result = await adapter.chat(_request(reasoning_effort="high"), _binding())

    assert result.text == "## Auth\n\nGrounded narrative citing src/auth.rs:10."
    assert result.provider == "codex"
    assert result.model == "gpt-5.5"
    assert result.tool_use_count == 2
    assert result.tools == {"command_execution": 2}
    assert result.applied_reasoning_effort == "high"
    assert result.stop_reason == "completed"


async def test_codex_adapter_falls_back_to_stream_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        # No last-message file; stream has agent_message.
        return (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"Stream fallback narrative."}}'
        )

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = CodexSpawnToolChatAdapter(command_path="codex")

    result = await adapter.chat(_request(), _binding())

    assert result.text == "Stream fallback narrative."


async def test_codex_adapter_rejects_mutation_under_readonly_policy() -> None:
    adapter = CodexSpawnToolChatAdapter(command_path="codex")
    request = _request(tool_policy=ToolPolicy(cli="gcode", tools=("search", "index")))

    with pytest.raises(ToolPolicyError):
        await adapter.chat(request, _binding())


async def test_codex_adapter_hard_fails_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        return ""

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = CodexSpawnToolChatAdapter(command_path="codex")

    with pytest.raises(RuntimeError, match="no final message"):
        await adapter.chat(_request(), _binding())


# --- Droid adapter ---------------------------------------------------------


def _droid_binding() -> CapabilityBinding:
    return _binding(provider="droid", style=AIAdapterStyle.CLI)


def test_droid_build_command_enables_execute_and_uses_gcode_prompt() -> None:
    adapter = DroidSpawnToolChatAdapter(command_path="droid")
    request = _request(reasoning_effort="high")

    command = adapter._build_command(request, model="gpt-5.5")

    assert command[0] == "droid"
    assert "exec" in command
    # `--auto high` lets droid's Execute tool run the gcode binary.
    assert command[command.index("--auto") + 1] == "high"
    assert command[command.index("--output-format") + 1] == "stream-json"
    disabled = command[command.index("--disabled-tools") + 1]
    # Execute must NOT be disabled — the agent needs shell access for gcode.
    assert "Execute" not in disabled
    # But file-mutation and automation tools ARE disabled (defense-in-depth).
    for blocked in ("Edit", "Create", "ApplyPatch", "Task"):
        assert blocked in disabled
    assert command[command.index("--model") + 1] == "gpt-5.5"
    # The composed prompt uses gcode-direct, not gobby-index.
    assert "gcode" in command[-1]
    assert "gobby-index" not in command[-1]


async def test_droid_adapter_captures_narrative_and_counts_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        captured["env"] = env_overrides
        captured["provider"] = provider_name
        assert ".gobby/bin" in env_overrides["PATH"]
        return _DROID_STREAM

    monkeypatch.setattr(spawn, "_seed_droid_factory_state", lambda *a, **k: None)
    monkeypatch.setattr(spawn, "_droid_isolated_env", lambda base, home: {"HOME": str(home)})
    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = DroidSpawnToolChatAdapter(command_path="droid")

    result = await adapter.chat(_request(reasoning_effort="high"), _droid_binding())

    assert result.text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert result.provider == "droid"
    assert result.tool_use_count == 2
    assert result.tools == {"Execute": 2}
    assert result.stop_reason == "completed"
    assert captured["provider"] == "Droid tool_chat"


async def test_droid_adapter_rejects_mutation_under_readonly_policy() -> None:
    adapter = DroidSpawnToolChatAdapter(command_path="droid")
    request = _request(tool_policy=ToolPolicy(cli="gcode", tools=("search", "index")))

    with pytest.raises(ToolPolicyError):
        await adapter.chat(request, _droid_binding())


async def test_droid_adapter_hard_fails_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        return '{"type":"system","subtype":"init"}\n{"type":"reasoning","text":"hmm"}'

    monkeypatch.setattr(spawn, "_seed_droid_factory_state", lambda *a, **k: None)
    monkeypatch.setattr(spawn, "_droid_isolated_env", lambda base, home: {"HOME": str(home)})
    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = DroidSpawnToolChatAdapter(command_path="droid")

    with pytest.raises(RuntimeError, match="no final message"):
        await adapter.chat(_request(), _droid_binding())


# --- Grok adapter ----------------------------------------------------------


def _grok_binding() -> CapabilityBinding:
    return _binding(provider="grok", style=AIAdapterStyle.ACP)


def test_grok_build_command_uses_sandbox_json_and_gcode_prompt() -> None:
    adapter = GrokSpawnToolChatAdapter(command_path="grok")
    request = _request(reasoning_effort="high")

    command = adapter._build_command(request, model="grok-4")

    assert command[0] == "grok"
    assert "--single" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--sandbox") + 1] == "workspace"
    assert "--always-approve" in command
    assert "--no-subagents" in command
    assert command[command.index("--model") + 1] == "grok-4"
    disabled = command[command.index("--disallowed-tools") + 1]
    for blocked in ("Edit", "Write", "Task"):
        assert blocked in disabled
    # The prompt is the --single argument (positional after --single).
    single_idx = command.index("--single")
    prompt = command[single_idx + 1]
    assert "gcode" in prompt
    assert "gobby-index" not in prompt


async def test_grok_adapter_captures_narrative_from_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        assert ".gobby/bin" in env_overrides["PATH"]
        return '{"text":"## Auth\\n\\nNarrative citing src/auth.rs:10.","stopReason":"EndTurn"}'

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    # No sessionId in output -> no session dir lookup -> tool_use_count stays 0.
    monkeypatch.setattr(spawn, "_resolve_grok_session_dir", lambda sid, wd: None)
    adapter = GrokSpawnToolChatAdapter(command_path="grok")

    result = await adapter.chat(_request(), _grok_binding())

    assert result.text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert result.provider == "grok"
    assert result.stop_reason == "completed"
    # Graceful degradation: no session dir -> zero tool counts.
    assert result.tool_use_count == 0
    assert result.tools == {}


async def test_grok_adapter_extracts_tool_counts_from_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the JSON output includes sessionId, tool counts come from the session dir."""
    session_id = "session-abc-123"
    fake_session = tmp_path / "session-dir"
    fake_session.mkdir()
    (fake_session / "updates.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "tool_call",
                                "title": "run_terminal_command",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "tool_call",
                                "title": "run_terminal_command",
                            },
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        return json.dumps(
            {
                "text": "## Auth\n\nNarrative citing src/auth.rs:10.",
                "stopReason": "EndTurn",
                "sessionId": session_id,
            }
        )

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    monkeypatch.setattr(
        spawn,
        "_resolve_grok_session_dir",
        lambda sid, wd: fake_session if sid == session_id else None,
    )
    adapter = GrokSpawnToolChatAdapter(command_path="grok")

    result = await adapter.chat(_request(), _grok_binding())

    assert result.text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert result.tool_use_count == 2
    assert result.tools == {"run_terminal_command": 2}
    assert result.turns == 2


async def test_grok_adapter_hard_fails_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        return '{"text":"","stopReason":"EndTurn"}'

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = GrokSpawnToolChatAdapter(command_path="grok")

    with pytest.raises(RuntimeError, match="no final message"):
        await adapter.chat(_request(), _grok_binding())


# --- Qwen adapter ----------------------------------------------------------


def _qwen_binding() -> CapabilityBinding:
    return _binding(provider="qwen", style=AIAdapterStyle.ACP)


def test_qwen_build_command_uses_sandbox_yolo_and_stream_json() -> None:
    adapter = QwenSpawnToolChatAdapter(command_path="qwen")
    request = _request(reasoning_effort="high")

    command = adapter._build_command(request, model="qwen3-coder")

    assert command[0] == "qwen"
    assert "--sandbox" in command
    assert command[command.index("--approval-mode") + 1] == "yolo"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--bare" in command
    assert command[command.index("--model") + 1] == "qwen3-coder"
    # The prompt is the final positional argument.
    assert "gcode" in command[-1]
    assert "gobby-index" not in command[-1]


async def test_qwen_adapter_captures_narrative_and_counts_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qwen_stream = "\n".join(
        [
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"tool_use","name":"run_shell_command"}]}}',
            '{"type":"result","result":"## Auth\\n\\nNarrative citing src/auth.rs:10."}',
        ]
    )
    captured: dict[str, object] = {}

    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        captured["env"] = env_overrides
        assert env_overrides.get("QWEN_CODE_SUPPRESS_YOLO_WARNING") == "1"
        assert env_overrides.get("SEATBELT_PROFILE") == "gobby-open"
        assert ".gobby/bin" in env_overrides["PATH"]
        # Verify the seatbelt profile was written before the temp dir is cleaned up.
        profile_path = neutral_cwd / ".qwen" / "sandbox-macos-gobby-open.sb"
        assert profile_path.exists(), f"Seatbelt profile not found at {profile_path}"
        content = profile_path.read_text(encoding="utf-8")
        assert "(version 1)" in content
        assert "file-write*" in content
        return qwen_stream

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = QwenSpawnToolChatAdapter(command_path="qwen")

    result = await adapter.chat(_request(), _qwen_binding())

    assert result.text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert result.provider == "qwen"
    assert result.tool_use_count == 1
    assert result.tools == {"run_shell_command": 1}


async def test_qwen_adapter_hard_fails_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        return '{"type":"system"}\n{"type":"result","result":""}'

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = QwenSpawnToolChatAdapter(command_path="qwen")

    with pytest.raises(RuntimeError, match="no final message"):
        await adapter.chat(_request(), _qwen_binding())


def test_prepare_qwen_sandbox_profile_writes_correct_file(tmp_path: Path) -> None:
    """The seatbelt profile file is written to .qwen/ with the right name and content."""
    from gobby.ai._tool_chat_spawn import _QWEN_SEATBELT_PROFILE_NAME, _prepare_qwen_sandbox_profile

    _prepare_qwen_sandbox_profile(tmp_path)

    profile_path = tmp_path / ".qwen" / f"sandbox-macos-{_QWEN_SEATBELT_PROFILE_NAME}.sb"
    assert profile_path.exists()
    content = profile_path.read_text(encoding="utf-8")
    assert "(version 1)" in content
    assert "(allow default)" in content
    assert "(deny file-write*)" in content
    assert "TARGET_DIR" in content


# --- ACP composite adapter -------------------------------------------------


async def test_acp_adapter_dispatches_to_grok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        return '{"text":"Grok narrative.","stopReason":"EndTurn"}'

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)

    class FakeConfig:
        class ai:
            class generation:
                timeout_seconds = 300.0

                class local:
                    endpoints = {}

    adapter = ACPSpawnToolChatAdapter(FakeConfig())  # type: ignore[arg-type]
    result = await adapter.chat(_request(), _grok_binding())

    assert result.text == "Grok narrative."
    assert result.provider == "grok"


async def test_acp_adapter_dispatches_to_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        return '{"type":"result","result":"Qwen narrative."}'

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)

    class FakeConfig:
        class ai:
            class generation:
                timeout_seconds = 300.0

                class local:
                    endpoints = {}

    adapter = ACPSpawnToolChatAdapter(FakeConfig())  # type: ignore[arg-type]
    result = await adapter.chat(_request(), _qwen_binding())

    assert result.text == "Qwen narrative."
    assert result.provider == "qwen"


async def test_acp_adapter_rejects_unknown_provider() -> None:
    class FakeConfig:
        class ai:
            class generation:
                timeout_seconds = 300.0

                class local:
                    endpoints = {}

    adapter = ACPSpawnToolChatAdapter(FakeConfig())  # type: ignore[arg-type]
    bad_binding = _binding(provider="unknown", style=AIAdapterStyle.ACP)

    with pytest.raises(ValueError, match="No ACP tool_chat adapter"):
        await adapter.chat(_request(), bad_binding)
