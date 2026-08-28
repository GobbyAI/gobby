"""Spawn-path tests for the host-native SRT wrapper."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents import spawn_executor
from gobby.agents.kill import pid_matches_agent_identity
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_executor import execute_spawn
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.srt_runtime import SandboxLaunch, SrtRuntimeError
from gobby.agents.tmux.spawner import _infer_auth_cli
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit


def test_srt_runner_uses_portable_preflight_and_finally_cleanup() -> None:
    runner = (
        Path(__file__).resolve().parents[2] / "src" / "gobby" / "agents" / "srt_runner.mjs"
    ).read_text(encoding="utf-8")

    assert "[process.execPath, '--version']" in runner
    assert "/usr/bin/true" not in runner
    cleanup = runner.split("} finally {", maxsplit=1)[1]
    assert "process.off(signal, handler)" in cleanup
    assert "unsubscribe()" in cleanup
    assert "await SandboxManager.reset()" in cleanup


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
    runtime_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_runtime_spawn"
    ]

    assert wrap_calls == []
    assert len(runtime_calls) == 1
    runtime_tree = ast.parse(inspect.getsource(spawn_executor._runtime_spawn))
    runtime_wraps = [
        node
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "wrap_provider_command"
    ]
    assert len(runtime_wraps) == 1


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
    process = MagicMock()
    process.cmdline.return_value = [
        "/managed/node",
        "/managed/runner.mjs",
        "--settings",
        "/policy",
        "--",
        "/opt/claude/versions/2.1.220",
        "--session-id",
        "child-session",
    ]

    assert await pid_matches_agent_identity(
        123,
        provider="claude",
        session_id="child-session",
        process_factory=MagicMock(return_value=process),
    )


@pytest.mark.asyncio
async def test_droid_command_is_wrapped_once_after_srt_preflight() -> None:
    session_manager = MagicMock()
    run_manager = MagicMock()
    request = SpawnRequest(
        prompt="work",
        cwd="/workspace",
        provider="droid",
        session_id="parent",
        run_id="requested-run",
        parent_session_id="parent",
        project_id="project",
        session_manager=session_manager,
        run_manager=run_manager,
        sandbox_config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
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
        provider_executable="/opt/droid/versions/0.48.0",
        node_path="/managed/node",
        runner_path="/managed/runner.mjs",
    )
    spawner = MagicMock()
    spawner.spawn.return_value = SimpleNamespace(
        success=True,
        pid=123,
        terminal_type="tmux",
        terminal_id="gobby-agent",
        tmux_socket_name="gobby",
        tmux_socket_path="/tmp/gobby.sock",
        error=None,
        message=None,
    )

    with (
        patch("gobby.agents.spawn_executor.shutil.which", return_value="/usr/local/bin/droid"),
        patch("gobby.agents.spawn_executor.prepare_terminal_spawn", return_value=spawn_context),
        patch(
            "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
            return_value=launch,
        ) as prepare_launch,
        patch("gobby.agents.spawn_executor_providers._record_resume_launch_details"),
        patch("gobby.agents.spawn_executor_providers.pre_approve_directory"),
    ):
        request.prepared_spawn = cast("PreparedSpawn", spawn_context)
        result = await execute_spawn(request)

    assert result.success is True
    prepare_launch.assert_awaited_once()
    assert prepare_launch.call_args.kwargs["resolver"] is None
    from tests.agents.test_spawn_executor import _spawn_kwargs

    spawn_kwargs = _spawn_kwargs(request)
    command = spawn_kwargs["command"]
    assert command[:7] == [
        "/managed/node",
        "/managed/runner.mjs",
        "--settings",
        "/policy/settings.json",
        "--violations",
        "/policy/violations.jsonl",
        "--",
    ]
    assert command[7] == "/opt/droid/versions/0.48.0"
    assert "droid" not in command
    assert spawn_kwargs["auth_cli"] == "droid"


@pytest.mark.asyncio
async def test_srt_preflight_failure_prevents_tmux_spawn() -> None:
    session_manager = MagicMock()
    run_manager = MagicMock()
    request = SpawnRequest(
        prompt="work",
        cwd="/workspace",
        provider="droid",
        session_id="parent",
        run_id="requested-run",
        parent_session_id="parent",
        project_id="project",
        session_manager=session_manager,
        run_manager=run_manager,
        sandbox_config=SandboxConfig(enabled=True, backend="srt", allow_network=False),
        prepared_spawn=prepared_spawn(
            session_id="child",
            agent_run_id="actual-run",
            env_vars={"GOBBY_SESSION_ID": "child"},
        ),
        terminal_backend="tmux",
    )
    spawn_context = SimpleNamespace(
        session_id="child",
        agent_run_id="actual-run",
        env_vars={"GOBBY_SESSION_ID": "child"},
    )

    with (
        patch("gobby.agents.spawn_executor.shutil.which", return_value="/usr/local/bin/droid"),
        patch("gobby.agents.spawn_executor.prepare_terminal_spawn", return_value=spawn_context),
        patch(
            "gobby.agents.spawn_executor_providers.prepare_sandbox_launch",
            side_effect=SrtRuntimeError("invalid policy"),
        ),
    ):
        result = await execute_spawn(request)

    assert result.success is False
    assert result.status == "failed"
    assert "failed closed" in (result.error or "")
    run_manager.fail.assert_called_once_with(
        "actual-run",
        "Sandbox startup failed closed for droid: invalid policy",
    )
    from tests.agents.test_spawn_executor import _runtime_of

    assert _runtime_of(request).last_request is None
