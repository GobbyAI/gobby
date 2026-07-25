"""Tests for the remaining spawn-based tool_chat adapters."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from gobby.ai import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.ai import _tool_chat_spawn as spawn
from gobby.ai._tool_chat_contracts import (
    ToolChatRequest,
    ToolChatResult,
    ToolLoopLimits,
    ToolPolicy,
)
from gobby.ai._tool_chat_spawn import (
    ACPSpawnToolChatAdapter,
    GrokSpawnToolChatAdapter,
    QwenSpawnToolChatAdapter,
    compose_gcode_direct_prompt,
    parse_grok_session_signals,
    parse_qwen_stream,
)
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def _binding(
    provider: str = "codex", style: AIAdapterStyle = AIAdapterStyle.DAEMON
) -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider=provider,
        adapter_style=style,
        available=True,
        models=("gpt-5.6-sol",),
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


def test_tool_chat_result_defaults_optional_text_and_turns() -> None:
    result = ToolChatResult(text=None)

    assert result.text is None
    assert result.turns is None


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


def test_compose_gcode_direct_prompt_quotes_project_path() -> None:
    project_path = "/repo with spaces/$(unsafe)"
    request = _request(project_path=project_path)

    prompt = compose_gcode_direct_prompt(request)

    assert f"--project {shlex.quote(project_path)}" in prompt


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
            '"num_turns":4,"usage":{"input_tokens":100,"output_tokens":50}}',
        ]
    )

    text, total, breakdown, turns, usage, error_message = parse_qwen_stream(stream)

    assert text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert total == 2
    assert breakdown == {"run_shell_command": 2}
    assert turns == 4
    assert usage == {"input_tokens": 100, "output_tokens": 50}
    assert error_message is None


def test_parse_qwen_stream_extracts_limit_result_without_narrative() -> None:
    stream = "\n".join(
        [
            '{"type":"assistant","message":{"content":'
            '[{"type":"tool_use","name":"run_shell_command"}]}}',
            '{"type":"result","subtype":"error_during_execution","is_error":true,'
            '"num_turns":6,"usage":{"input_tokens":120,"output_tokens":20},'
            '"error":{"message":"tool-call budget of 1 exceeded (--max-tool-calls)"}}',
        ]
    )

    text, total, breakdown, turns, usage, error_message = parse_qwen_stream(stream)

    assert text is None
    assert total == 1
    assert breakdown == {"run_shell_command": 1}
    assert turns == 6
    assert usage == {"input_tokens": 120, "output_tokens": 20}
    assert error_message == "tool-call budget of 1 exceeded (--max-tool-calls)"


def test_parse_qwen_stream_skips_non_json_lines() -> None:
    text, total, _, turns, usage, error_message = parse_qwen_stream("warning text\n{}\n")

    assert text is None
    assert total == 0
    assert turns is None
    assert usage is None
    assert error_message is None


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
    (tmp_path / "signals.json").write_text(
        json.dumps({"toolCallCount": 2, "turnCount": 5}),
        encoding="utf-8",
    )

    total, breakdown, turns = parse_grok_session_signals(tmp_path)

    assert total == 2
    assert breakdown == {"run_terminal_command": 1, "read_file": 1}
    assert turns == 5


def test_parse_grok_session_signals_falls_back_to_signals_json(
    tmp_path: Path,
) -> None:
    """When updates.jsonl has no tool_call events, signals.json provides the total."""
    (tmp_path / "updates.jsonl").write_text(
        json.dumps({"method": "session/update", "params": {"update": {"sessionUpdate": "text"}}}),
        encoding="utf-8",
    )
    (tmp_path / "signals.json").write_text(
        json.dumps(
            {
                "toolCallCount": 3,
                "turnCount": 4,
                "toolsUsed": ["run_terminal_command", "read_file"],
            }
        ),
        encoding="utf-8",
    )

    total, breakdown, turns = parse_grok_session_signals(tmp_path)

    assert total == 3
    assert "run_terminal_command" in breakdown
    assert "read_file" in breakdown
    assert turns == 4


def test_parse_grok_session_signals_returns_zeros_when_files_missing(
    tmp_path: Path,
) -> None:
    total, breakdown, turns = parse_grok_session_signals(tmp_path)

    assert total == 0
    assert breakdown == {}
    assert turns is None


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


# --- Grok adapter ----------------------------------------------------------


def _grok_binding() -> CapabilityBinding:
    return _binding(provider="grok", style=AIAdapterStyle.ACP)


def test_grok_build_command_uses_sandbox_json_and_gcode_prompt() -> None:
    adapter = GrokSpawnToolChatAdapter(command_path="grok")
    request = _request(
        reasoning_effort="high",
        limits=ToolLoopLimits(max_turns=4),
    )

    command = adapter._build_command(request, model="grok-4")

    assert command[0] == "grok"
    assert "--single" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--sandbox") + 1] == "workspace"
    assert "--always-approve" in command
    assert "--no-subagents" in command
    assert command[command.index("--model") + 1] == "grok-4"
    assert command[command.index("--max-turns") + 1] == "4"
    disabled = command[command.index("--disallowed-tools") + 1]
    for blocked in ("Edit", "Write", "Task"):
        assert blocked in disabled
    # The prompt is the --single argument (positional after --single).
    single_idx = command.index("--single")
    prompt = command[single_idx + 1]
    assert "gcode" in prompt
    assert "gobby-index" not in prompt


def test_grok_build_command_omits_unlimited_max_turns() -> None:
    adapter = GrokSpawnToolChatAdapter(command_path="grok")

    command = adapter._build_command(
        _request(limits=ToolLoopLimits(max_turns=None)),
        model=None,
    )

    assert "--max-turns" not in command


@pytest.mark.asyncio
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


@pytest.mark.parametrize(
    ("provider_stop_reason", "expected"),
    [
        ("EndTurn", "completed"),
        ("MaxTurnRequests", "max_turns"),
        ("MaxTokens", None),
        ("Refusal", None),
        ("Cancelled", None),
        (None, None),
        ("FutureReason", None),
    ],
)
@pytest.mark.asyncio
async def test_grok_adapter_normalizes_verified_stop_reasons(
    monkeypatch: pytest.MonkeyPatch,
    provider_stop_reason: str | None,
    expected: str | None,
) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        payload: dict[str, str] = {"text": "Grounded result."}
        if provider_stop_reason is not None:
            payload["stopReason"] = provider_stop_reason
        return json.dumps(payload)

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = GrokSpawnToolChatAdapter(command_path="grok")

    result = await adapter.chat(_request(), _grok_binding())

    assert result.stop_reason == expected


@pytest.mark.asyncio
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
    (fake_session / "signals.json").write_text(
        json.dumps({"toolCallCount": 2, "turnCount": 7}),
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
    assert result.turns == 7


@pytest.mark.asyncio
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
    request = _request(
        reasoning_effort="high",
        limits=ToolLoopLimits(max_turns=4, max_tool_calls=9),
    )

    command = adapter._build_command(request, model="qwen3-coder")

    assert command[0] == "qwen"
    assert "--sandbox" in command
    assert command[command.index("--approval-mode") + 1] == "yolo"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert command[command.index("--max-session-turns") + 1] == "4"
    assert command[command.index("--max-tool-calls") + 1] == "9"
    assert "--bare" in command
    assert command[command.index("--model") + 1] == "qwen3-coder"
    # The prompt is the final positional argument.
    assert "gcode" in command[-1]
    assert "gobby-index" not in command[-1]


@pytest.mark.asyncio
async def test_qwen_adapter_captures_narrative_and_counts_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qwen_stream = "\n".join(
        [
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"tool_use","name":"run_shell_command"}]}}',
            '{"type":"result","result":"## Auth\\n\\nNarrative citing src/auth.rs:10.",'
            '"num_turns":4,"usage":{"input_tokens":100,"output_tokens":20}}',
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
        accepted_exit_codes: frozenset[int] | None = None,
    ) -> tuple[str, str, int]:
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
        assert accepted_exit_codes == frozenset({53, 55})
        return qwen_stream, "", 0

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = QwenSpawnToolChatAdapter(command_path="qwen")

    result = await adapter.chat(_request(), _qwen_binding())

    assert result.text == "## Auth\n\nNarrative citing src/auth.rs:10."
    assert result.provider == "qwen"
    assert result.tool_use_count == 1
    assert result.turns == 4
    assert result.tools == {"run_shell_command": 1}
    assert result.usage == {"input_tokens": 100, "output_tokens": 20}
    assert result.stop_reason == "completed"


@pytest.mark.parametrize(
    ("returncode", "message", "stderr", "expected_stop_reason"),
    [
        (53, "session turn limit exceeded (--max-session-turns)", "", "max_turns"),
        (55, "tool-call budget of 1 exceeded (--max-tool-calls)", "", "max_tool_calls"),
        (55, "wall-clock budget of 10s exceeded (--max-wall-time)", "", "timeout"),
        (55, None, "tool-call budget exceeded (--max-tool-calls)", "max_tool_calls"),
    ],
)
@pytest.mark.asyncio
async def test_qwen_adapter_maps_limit_exits_to_typed_results(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    message: str | None,
    stderr: str,
    expected_stop_reason: str,
) -> None:
    stream = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 6,
            "usage": {"input_tokens": 120, "output_tokens": 20},
            "error": {"message": message},
        }
    )

    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
        accepted_exit_codes: frozenset[int] | None = None,
    ) -> tuple[str, str, int]:
        assert accepted_exit_codes == frozenset({53, 55})
        return stream, stderr, returncode

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = QwenSpawnToolChatAdapter(command_path="qwen")

    result = await adapter.chat(_request(), _qwen_binding())

    assert result.text is None
    assert result.stop_reason == expected_stop_reason
    assert result.turns == 6
    assert result.usage == {"input_tokens": 120, "output_tokens": 20}
    assert result.budget_exhausted is True


@pytest.mark.asyncio
async def test_qwen_adapter_rejects_ambiguous_exit_55(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "num_turns": 2,
            "error": {"message": "fatal budget exceeded"},
        }
    )

    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
        accepted_exit_codes: frozenset[int] | None = None,
    ) -> tuple[str, str, int]:
        return stream, "FatalBudgetExceededError", 55

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = QwenSpawnToolChatAdapter(command_path="qwen")

    with pytest.raises(RuntimeError, match="ambiguous.*exit code 55"):
        await adapter.chat(_request(), _qwen_binding())


@pytest.mark.asyncio
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
        accepted_exit_codes: frozenset[int] | None = None,
    ) -> tuple[str, str, int]:
        return '{"type":"system"}\n{"type":"result","result":""}', "", 0

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


@pytest.mark.asyncio
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

    adapter = ACPSpawnToolChatAdapter(DaemonConfig())
    result = await adapter.chat(_request(), _grok_binding())

    assert result.text == "Grok narrative."
    assert result.provider == "grok"


@pytest.mark.asyncio
async def test_acp_adapter_dispatches_to_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
        accepted_exit_codes: frozenset[int] | None = None,
    ) -> tuple[str, str, int]:
        return '{"type":"result","result":"Qwen narrative."}', "", 0

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)

    adapter = ACPSpawnToolChatAdapter(DaemonConfig())
    result = await adapter.chat(_request(), _qwen_binding())

    assert result.text == "Qwen narrative."
    assert result.provider == "qwen"


@pytest.mark.asyncio
async def test_acp_adapter_rejects_unknown_provider() -> None:
    adapter = ACPSpawnToolChatAdapter(DaemonConfig())
    bad_binding = _binding(provider="unknown", style=AIAdapterStyle.ACP)

    with pytest.raises(ValueError, match="No ACP tool_chat adapter"):
        await adapter.chat(_request(), bad_binding)
