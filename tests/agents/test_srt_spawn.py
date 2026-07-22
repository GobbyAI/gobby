"""Spawn-path tests for the host-native SRT wrapper."""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents import spawn_executor
from gobby.agents.kill import pid_matches_agent_identity
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_executor import execute_spawn
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.srt_runtime import SandboxLaunch, SrtRuntimeError
from gobby.agents.tmux.spawner import _infer_auth_cli


@pytest.mark.parametrize(
    "spawn_name",
    [
        "_spawn_claude_terminal",
        "_spawn_codex_terminal",
        "_spawn_qwen_terminal",
        "_spawn_grok_terminal",
        "_spawn_droid_terminal",
    ],
)
def test_every_managed_provider_wraps_the_completed_command_once(spawn_name: str) -> None:
    tree = ast.parse(inspect.getsource(getattr(spawn_executor, spawn_name)))
    wrap_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wrap"
    ]

    assert len(wrap_calls) == 1


def test_auth_cli_inference_looks_through_srt_wrapper() -> None:
    command = [
        "/managed/node",
        "/managed/runner.mjs",
        "--settings",
        "/policy/settings.json",
        "--violations",
        "/policy/violations.jsonl",
        "--",
        "/usr/local/bin/codex",
        "exec",
    ]

    assert _infer_auth_cli(command) == "codex"


@pytest.mark.asyncio
async def test_pane_pid_identity_accepts_provider_inside_srt_argv() -> None:
    async def fake_run_subprocess(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        return (
            0,
            "/managed/node /managed/runner.mjs --settings /policy -- "
            "claude --session-id child-session",
            "",
        )

    assert await pid_matches_agent_identity(
        123,
        provider="claude",
        session_id="child-session",
        run_subprocess=fake_run_subprocess,
    )


@pytest.mark.asyncio
async def test_droid_command_is_wrapped_once_after_srt_preflight() -> None:
    session_manager = MagicMock()
    session_manager._storage = SimpleNamespace(db=MagicMock())
    request = SpawnRequest(
        prompt="work",
        cwd="/workspace",
        provider="droid",
        session_id="parent",
        run_id="requested-run",
        parent_session_id="parent",
        project_id="project",
        session_manager=session_manager,
        sandbox_config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
    )
    spawn_context = SimpleNamespace(
        session_id="child",
        agent_run_id="actual-run",
        env_vars={"GOBBY_SESSION_ID": "child"},
    )
    launch = SandboxLaunch(
        backend="srt",
        enforced=True,
        runtime_version="0.0.66",
        policy_hash="hash",
        policy_path="/policy/settings.json",
        violation_path="/policy/violations.jsonl",
        node_path="/managed/node",
        runner_path="/managed/runner.mjs",
    )
    spawner = MagicMock()
    spawner.spawn.return_value = SimpleNamespace(
        success=True,
        pid=123,
        terminal_type="tmux",
        tmux_session_name="gobby-agent",
        tmux_socket_name="gobby",
        tmux_socket_path="/tmp/gobby.sock",
        error=None,
        message=None,
    )

    with (
        patch("gobby.agents.spawn_executor.shutil.which", return_value="/usr/local/bin/droid"),
        patch("gobby.agents.spawn_executor.prepare_terminal_spawn", return_value=spawn_context),
        patch(
            "gobby.agents.spawn_executor.prepare_sandbox_launch",
            return_value=launch,
        ) as prepare_launch,
        patch("gobby.agents.spawn_executor._record_resume_launch_details"),
        patch("gobby.agents.spawn_executor._tmux_spawner_for_request", return_value=spawner),
        patch("gobby.agents.spawn_executor.pre_approve_directory"),
    ):
        result = await execute_spawn(request)

    assert result.success is True
    prepare_launch.assert_awaited_once()
    assert prepare_launch.call_args.kwargs["resolver"] is None
    command = spawner.spawn.call_args.kwargs["command"]
    assert command[:7] == [
        "/managed/node",
        "/managed/runner.mjs",
        "--settings",
        "/policy/settings.json",
        "--violations",
        "/policy/violations.jsonl",
        "--",
    ]
    assert sum(1 for value in command if value == "droid") == 1


@pytest.mark.asyncio
async def test_srt_preflight_failure_prevents_tmux_spawn() -> None:
    session_manager = MagicMock()
    session_manager._storage = SimpleNamespace(db=MagicMock())
    request = SpawnRequest(
        prompt="work",
        cwd="/workspace",
        provider="droid",
        session_id="parent",
        run_id="requested-run",
        parent_session_id="parent",
        project_id="project",
        session_manager=session_manager,
        sandbox_config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
    )
    spawn_context = SimpleNamespace(
        session_id="child",
        agent_run_id="actual-run",
        env_vars={"GOBBY_SESSION_ID": "child"},
    )
    spawner = MagicMock()

    with (
        patch("gobby.agents.spawn_executor.shutil.which", return_value="/usr/local/bin/droid"),
        patch("gobby.agents.spawn_executor.prepare_terminal_spawn", return_value=spawn_context),
        patch(
            "gobby.agents.spawn_executor.prepare_sandbox_launch",
            side_effect=SrtRuntimeError("invalid policy"),
        ),
        patch("gobby.storage.agents.LocalAgentRunManager"),
        patch("gobby.agents.spawn_executor._tmux_spawner_for_request", return_value=spawner),
    ):
        result = await execute_spawn(request)

    assert result.success is False
    assert result.status == "failed"
    assert "failed closed" in (result.error or "")
    spawner.spawn.assert_not_called()
