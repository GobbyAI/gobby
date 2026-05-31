"""Tests for daemon-stop resume execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents import resume_executor
from gobby.agents.constants import CARGO_HOME, UV_CACHE_DIR
from gobby.storage.agents import AgentRun

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_resume_agent_run_persists_only_safe_cache_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume launch metadata should not retain provider/token-style env values."""
    original_run = AgentRun(
        id="run-old",
        parent_session_id="parent-old",
        provider="codex",
        prompt="Original prompt",
        status="daemon_stopped",
        created_at="2026-05-30T00:00:00Z",
        updated_at="2026-05-30T00:00:00Z",
        continuation_prompt="Continue",
    )
    resume_metadata: dict[str, Any] = {
        "provider": "codex",
        "provider_native_session_id": "native-123",
        "cwd": "/repo",
        "project_id": "proj-1",
        "parent_session_id": "parent-1",
        "machine_id": "machine-1",
        "env": {
            "PERSISTED": "old",
            CARGO_HOME: "/old/cargo",
            "GOBBY_SESSION_ID": "stale-child",
            "GOBBY_AGENT_RUN_ID": "stale-run",
        },
        "sandbox_env": {"SANDBOX": "enabled"},
    }
    persisted_metadata: list[tuple[str, dict[str, Any]]] = []

    def fake_prepare_terminal_spawn(**kwargs: Any) -> SimpleNamespace:
        agent_run_id = kwargs["agent_run_id"]
        return SimpleNamespace(
            session_id="child-new",
            env_vars={
                "GOBBY_SESSION_ID": "child-new",
                "GOBBY_AGENT_RUN_ID": agent_run_id,
                UV_CACHE_DIR: "/new/uv",
            },
        )

    class FakeSpawner:
        env: dict[str, str]

        def spawn(self, *, command: list[str], cwd: str, env: dict[str, str]) -> SimpleNamespace:
            self.env = env
            return SimpleNamespace(
                success=True,
                pid=123,
                tmux_session_name="tmux-new",
                error=None,
                message=None,
            )

    spawner = FakeSpawner()
    run_storage = MagicMock()
    run_storage.update_resume_metadata.side_effect = (
        lambda run_id, metadata: persisted_metadata.append((run_id, metadata))
    )
    runner = SimpleNamespace(
        child_session_manager=MagicMock(),
        run_storage=run_storage,
    )

    monkeypatch.setattr(resume_executor, "prepare_terminal_spawn", fake_prepare_terminal_spawn)
    monkeypatch.setattr(
        resume_executor,
        "build_cli_command",
        lambda **_kwargs: (["codex", "resume"], {}),
    )
    monkeypatch.setattr(resume_executor, "_tmux_spawner", lambda *_args: spawner)
    monkeypatch.setattr(resume_executor, "pre_approve_directory", lambda *_args: None)
    monkeypatch.setattr(resume_executor, "_fire_resume_started", lambda *_args: None)

    result = await resume_executor.resume_agent_run(
        original_run,
        resume_metadata=resume_metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    assert result.success is True
    assert "PERSISTED" not in spawner.env
    assert spawner.env[CARGO_HOME] == "/old/cargo"
    assert spawner.env[UV_CACHE_DIR] == "/new/uv"
    assert spawner.env["SANDBOX"] == "enabled"
    assert spawner.env["GOBBY_SESSION_ID"] == "child-new"
    assert spawner.env["GOBBY_AGENT_RUN_ID"] == result.run_id
    assert spawner.env["GOBBY_MACHINE_ID"] == "machine-1"
    assert len(persisted_metadata) == 1
    assert persisted_metadata[0][0] == result.run_id
    assert persisted_metadata[0][1]["env"] == {
        CARGO_HOME: "/old/cargo",
        UV_CACHE_DIR: "/new/uv",
    }


@pytest.mark.asyncio
async def test_resume_agent_run_rejects_relative_cwd_before_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run = AgentRun(
        id="run-old",
        parent_session_id="parent-old",
        provider="codex",
        prompt="Original prompt",
        status="daemon_stopped",
        created_at="2026-05-30T00:00:00Z",
        updated_at="2026-05-30T00:00:00Z",
        continuation_prompt="Continue",
    )
    resume_metadata: dict[str, Any] = {
        "provider": "codex",
        "provider_native_session_id": "native-123",
        "cwd": "relative/repo",
        "project_id": "proj-1",
        "parent_session_id": "parent-1",
    }
    preapproved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        resume_executor,
        "pre_approve_directory",
        lambda provider, cwd: preapproved.append((provider, cwd)),
    )

    result = await resume_executor.resume_agent_run(
        original_run,
        resume_metadata=resume_metadata,
        runner=SimpleNamespace(child_session_manager=MagicMock(), run_storage=MagicMock()),
        session_manager=MagicMock(),
    )

    assert result.success is False
    assert result.error == "resume_cwd_not_absolute"
    assert preapproved == []


@pytest.mark.asyncio
async def test_resume_agent_run_uses_workspace_mcp_config_for_claude(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Claude resumes must keep workspace MCP flags before the continuation prompt."""
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers":{"gobby":{"command":"uv","args":["run","gobby","mcp-server"]}}}'
    )
    original_run = AgentRun(
        id="run-old",
        parent_session_id="parent-old",
        provider="claude",
        prompt="Original prompt",
        status="daemon_stopped",
        created_at="2026-05-30T00:00:00Z",
        updated_at="2026-05-30T00:00:00Z",
        continuation_prompt="Continue",
    )
    resume_metadata: dict[str, Any] = {
        "provider": "claude",
        "provider_native_session_id": "native-claude",
        "cwd": str(tmp_path),
        "project_id": "proj-1",
        "parent_session_id": "parent-1",
        "machine_id": "machine-1",
        "model": "opus",
        "effective_reasoning_effort": "xhigh",
        "sandbox_args": ["--settings", "{}"],
    }

    def fake_prepare_terminal_spawn(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            session_id="child-new",
            env_vars={
                "GOBBY_SESSION_ID": "child-new",
                "GOBBY_AGENT_RUN_ID": kwargs["agent_run_id"],
            },
        )

    class FakeSpawner:
        command: list[str]

        def spawn(self, *, command: list[str], cwd: str, env: dict[str, str]) -> SimpleNamespace:
            self.command = command
            return SimpleNamespace(
                success=True,
                pid=123,
                tmux_session_name="tmux-new",
                error=None,
                message=None,
            )

    spawner = FakeSpawner()
    runner = SimpleNamespace(
        child_session_manager=MagicMock(),
        run_storage=MagicMock(),
    )

    monkeypatch.setattr(resume_executor, "prepare_terminal_spawn", fake_prepare_terminal_spawn)
    monkeypatch.setattr(resume_executor, "_tmux_spawner", lambda *_args: spawner)
    monkeypatch.setattr(resume_executor, "pre_approve_directory", lambda *_args: None)
    monkeypatch.setattr(resume_executor, "_fire_resume_started", lambda *_args: None)

    result = await resume_executor.resume_agent_run(
        original_run,
        resume_metadata=resume_metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    command = spawner.command
    mcp_config_path = str(tmp_path / ".mcp.json")
    prompt_index = command.index("Continue")
    assert result.success is True
    assert command[-1] == "Continue"
    assert command[0:3] == ["claude", "--resume", "native-claude"]
    assert command[command.index("--mcp-config") + 1] == mcp_config_path
    assert command.index("--strict-mcp-config") < command.index("--settings") < prompt_index


@pytest.mark.asyncio
async def test_resume_agent_run_reuses_persisted_claude_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Claude resumes should keep the exact MCP config selected at initial launch."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    persisted_dir = tmp_path / "persisted"
    persisted_dir.mkdir()
    persisted_mcp_path = persisted_dir / ".mcp.json"
    persisted_mcp_path.write_text(
        '{"mcpServers":{"gobby":{"command":"uv","args":["run","gobby","mcp-server"]}}}',
        encoding="utf-8",
    )
    original_run = AgentRun(
        id="run-old",
        parent_session_id="parent-old",
        provider="claude",
        prompt="Original prompt",
        status="daemon_stopped",
        created_at="2026-05-30T00:00:00Z",
        updated_at="2026-05-30T00:00:00Z",
        continuation_prompt="Continue",
    )
    resume_metadata: dict[str, Any] = {
        "provider": "claude",
        "provider_native_session_id": "native-claude",
        "cwd": str(cwd),
        "project_id": "proj-1",
        "parent_session_id": "parent-1",
        "machine_id": "machine-1",
        "mcp_path": str(persisted_mcp_path),
        "strict_mcp": True,
        "sandbox_args": ["--settings", "{}"],
    }
    persisted_metadata: list[tuple[str, dict[str, Any]]] = []

    def fake_prepare_terminal_spawn(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            session_id="child-new",
            env_vars={
                "GOBBY_SESSION_ID": "child-new",
                "GOBBY_AGENT_RUN_ID": kwargs["agent_run_id"],
            },
        )

    class FakeSpawner:
        command: list[str]

        def spawn(self, *, command: list[str], cwd: str, env: dict[str, str]) -> SimpleNamespace:
            self.command = command
            return SimpleNamespace(
                success=True,
                pid=123,
                tmux_session_name="tmux-new",
                error=None,
                message=None,
            )

    spawner = FakeSpawner()
    run_storage = MagicMock()
    run_storage.update_resume_metadata.side_effect = (
        lambda run_id, metadata: persisted_metadata.append((run_id, metadata))
    )
    runner = SimpleNamespace(
        child_session_manager=MagicMock(),
        run_storage=run_storage,
    )

    monkeypatch.setattr(resume_executor, "prepare_terminal_spawn", fake_prepare_terminal_spawn)
    monkeypatch.setattr(resume_executor, "_tmux_spawner", lambda *_args: spawner)
    monkeypatch.setattr(resume_executor, "pre_approve_directory", lambda *_args: None)
    monkeypatch.setattr(resume_executor, "_fire_resume_started", lambda *_args: None)

    result = await resume_executor.resume_agent_run(
        original_run,
        resume_metadata=resume_metadata,
        runner=runner,
        session_manager=MagicMock(),
    )

    command = spawner.command
    prompt_index = command.index("Continue")
    assert result.success is True
    assert command[command.index("--mcp-config") + 1] == str(persisted_mcp_path)
    assert command.index("--strict-mcp-config") < command.index("--settings") < prompt_index
    assert persisted_metadata[0][1]["mcp_path"] == str(persisted_mcp_path)
    assert persisted_metadata[0][1]["strict_mcp"] is True
