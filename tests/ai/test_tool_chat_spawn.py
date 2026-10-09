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
    compose_gcode_direct_prompt,
    parse_grok_session_signals,
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
async def test_acp_adapter_rejects_unknown_provider() -> None:
    adapter = ACPSpawnToolChatAdapter(DaemonConfig())
    bad_binding = _binding(provider="unknown", style=AIAdapterStyle.ACP)

    with pytest.raises(
        ValueError,
        match=r"No ACP tool_chat adapter for provider 'unknown'; expected 'grok'\.",
    ):
        await adapter.chat(_request(), bad_binding)
