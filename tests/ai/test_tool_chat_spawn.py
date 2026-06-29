"""Tests for the Family B spawn tool_chat adapters (Codex via ``codex exec``).

Covers the read-only ``gcode`` shim (whitelist enforcement, ``--project``
injection, invocation logging), the spawn-prompt preamble, tool-call accounting,
and the Codex adapter's command construction + result mapping with the external
spawn stubbed out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.ai import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.ai import _tool_chat_spawn as spawn
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolPolicy
from gobby.ai._tool_chat_spawn import (
    CodexSpawnToolChatAdapter,
    build_readonly_cli_shim,
    compose_spawn_prompt,
    count_tool_calls,
)
from gobby.ai._tool_chat_tools import ToolPolicyError


def _binding() -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider="codex",
        adapter_style=AIAdapterStyle.DAEMON,
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


def _install_stub_real_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make the shim exec a stub that echoes its argv, not the real gcode."""
    stub = tmp_path / "real-gcode"
    stub.write_text('#!/bin/sh\necho "REAL_CALLED $@"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(spawn, "_resolve_real_cli", lambda _cli: str(stub))
    return stub


def _build_shim(tmp_path: Path, policy: ToolPolicy) -> tuple[Path, Path]:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log_path = tmp_path / "tool-calls.log"
    log_path.write_text("", encoding="utf-8")
    shim = build_readonly_cli_shim(policy, "/repo", shim_dir=shim_dir, log_path=log_path)
    return shim, log_path


def test_shim_allows_whitelisted_injects_project_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub_real_cli(monkeypatch, tmp_path)
    policy = ToolPolicy(cli="gcode", tools=("search", "outline"))
    shim, log_path = _build_shim(tmp_path, policy)

    result = subprocess.run(
        [str(shim), "search", "auth"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "REAL_CALLED" in result.stdout
    assert "--project" in result.stdout
    assert "/repo" in result.stdout
    assert "search" in result.stdout
    assert log_path.read_text(encoding="utf-8").splitlines() == ["search"]


def test_shim_rejects_non_whitelisted_subcommand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub_real_cli(monkeypatch, tmp_path)
    policy = ToolPolicy(cli="gcode", tools=("search", "outline"))
    shim, log_path = _build_shim(tmp_path, policy)

    # `index` is a mutator and not in the policy — must be refused, not forwarded.
    result = subprocess.run([str(shim), "index", "."], capture_output=True, text=True, check=False)

    assert result.returncode == 2
    assert "not permitted" in result.stderr
    assert "REAL_CALLED" not in result.stdout
    assert log_path.read_text(encoding="utf-8") == ""


def test_shim_does_not_double_inject_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub_real_cli(monkeypatch, tmp_path)
    policy = ToolPolicy(cli="gcode", tools=("search",))
    shim, _ = _build_shim(tmp_path, policy)

    result = subprocess.run(
        [str(shim), "search", "--project", "/other", "auth"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    # Exactly one --project (the caller's), no injected duplicate.
    assert result.stdout.count("--project") == 1
    assert "/other" in result.stdout
    assert "/repo" not in result.stdout


def test_compose_spawn_prompt_includes_tools_and_no_project_rule() -> None:
    request = _request(system_prompt="You are a code historian.")

    prompt = compose_spawn_prompt(request)

    assert "You are a code historian." in prompt
    assert "Document the auth module." in prompt
    assert "search, outline" in prompt
    assert "do NOT pass" in prompt
    assert "gcode" in prompt


def test_count_tool_calls(tmp_path: Path) -> None:
    log_path = tmp_path / "log"
    log_path.write_text("search\noutline\nsearch\n\n", encoding="utf-8")

    total, breakdown = count_tool_calls(log_path)

    assert total == 3
    assert breakdown == {"search": 2, "outline": 1}


def test_count_tool_calls_missing_log(tmp_path: Path) -> None:
    assert count_tool_calls(tmp_path / "absent") == (0, {})


def test_codex_build_command_uses_network_sandbox_and_capture() -> None:
    adapter = CodexSpawnToolChatAdapter(command_path="codex")
    request = _request(reasoning_effort="high")
    output_path = Path("/tmp/last-message.txt")

    command = adapter._build_command(request, model="gpt-5.5", output_path=output_path)

    assert command[0] == "codex"
    assert "exec" in command
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--output-last-message") + 1] == str(output_path)
    assert command[command.index("--model") + 1] == "gpt-5.5"
    # The composed prompt is the final positional argument.
    assert "Document the auth module." in command[-1]


async def test_codex_adapter_captures_narrative_and_counts_tools(
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
        # Simulate the spawned agent: emit the final message and log two
        # read-only tool calls via the shim log the adapter prepared.
        (neutral_cwd / "last-message.txt").write_text(
            "## Auth\n\nGrounded narrative citing src/auth.rs:10.",
            encoding="utf-8",
        )
        log = neutral_cwd / "tool-calls.log"
        with log.open("a", encoding="utf-8") as handle:
            handle.write("search\noutline\n")
        # A read-only run leaves the shim dir on PATH ahead of the real CLI.
        assert env_overrides["PATH"].startswith(str(neutral_cwd / "shim"))
        return ""

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = CodexSpawnToolChatAdapter(command_path="codex")

    result = await adapter.chat(_request(reasoning_effort="high"), _binding())

    assert result.text == "## Auth\n\nGrounded narrative citing src/auth.rs:10."
    assert result.provider == "codex"
    assert result.model == "gpt-5.5"
    assert result.tool_use_count == 2
    assert result.tools == {"search": 1, "outline": 1}
    assert result.applied_reasoning_effort == "high"
    assert result.stop_reason == "completed"


async def test_codex_adapter_rejects_mutation_under_readonly_policy() -> None:
    adapter = CodexSpawnToolChatAdapter(command_path="codex")
    # `index` is a mutator; a read-only policy must be refused before any spawn.
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
        # Agent ran but produced no final message: leave last-message.txt absent.
        return ""

    monkeypatch.setattr(spawn, "_run_cli_text_generation_command", fake_run)
    adapter = CodexSpawnToolChatAdapter(command_path="codex")

    # No silent blank "completed" result — hard-fail so the caller surfaces it.
    with pytest.raises(RuntimeError, match="no final message"):
        await adapter.chat(_request(), _binding())
